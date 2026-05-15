from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from src.integrations.codex_skills import execution
from src.integrations.codex_skills.execution import (
    SkillPlatformHandlerRegistry,
    SkillServiceRegistry,
    resolve_skill_execution_config,
)
from src.integrations.codex_skills.parser import SkillParseError, parse_skill_file
from src.integrations.codex_skills.rust_contract import load_skill_runtime_contract


class SkillRuntimeRustContractTest(unittest.TestCase):
    def test_skill_runtime_contract_declares_policy_kernel_and_sandbox(self) -> None:
        contract = load_skill_runtime_contract()
        self.assertEqual(contract["component"], "maf_skill_runtime")
        self.assertEqual(contract["mode_env"], "MAF_RUST_SKILL_RUNTIME_MODE")
        self.assertEqual(contract["modes"], ["off", "shadow", "enforce"])
        self.assertIn("policy_kernel", contract["supported_features"])
        self.assertIn("sandbox_sidecar", contract["supported_features"])
        self.assertIn("pyo3_policy_facade", contract["supported_features"])
        self.assertEqual(contract["client_version"], "0.1.0")
        self.assertEqual(contract["min_client_version"], "0.1.0")
        self.assertEqual(contract["max_client_version"], "0.1.x")
        self.assertIn("artifact_policy", contract)
        self.assertIn("benchmark_policy", contract)
        self.assertIn("promotion_policy", contract)
        self.assertIn("ops_policy", contract)
        self.assertIn("decommission_policy", contract)

    def test_skill_owned_rust_metadata_is_not_execution_mode_or_secret_carrier(self) -> None:
        contract = load_skill_runtime_contract()
        self.assertEqual(contract["allowed_rust_adapters"], ["pyo3", "binary", "sidecar"])
        self.assertIn("delegated_main_agent", contract["allowed_execution_modes"])
        self.assertNotIn("rust", contract["allowed_execution_modes"])
        for key in ["secret", "endpoint", "socket_path", "mtls_key", "download_url", "local_path"]:
            self.assertIn(key, contract["forbidden_x_runtime_rust_keys"])

    def test_parser_rejects_forbidden_skill_owned_rust_authority_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "SKILL.md"
            skill.write_text(
                "---\n"
                "name: rust-owned-bad\n"
                "description: bad metadata\n"
                "x_runtime:\n"
                "  rust:\n"
                "    adapter: pyo3\n"
                "    endpoint: http://127.0.0.1:1\n"
                "---\n"
                "Body\n",
                encoding="utf-8",
            )
            with self.assertRaises(SkillParseError):
                parse_skill_file(skill)

    def test_execution_defaults_and_answer_modes_come_from_rust_contract(self) -> None:
        contract = load_skill_runtime_contract()
        self.assertEqual(contract["default_execution_modes"], {"instruction_only": "delegated_main_agent", "scripted": "python_subprocess"})
        self.assertEqual(
            contract["default_answer_mode_by_execution_mode"],
            {"delegated_main_agent": "direct", "python_subprocess": "requires_finalizer"},
        )
        self.assertEqual(contract["answer_mode_required_execution_modes"], ["platform_service"])
        self.assertEqual(contract["allowed_answer_modes"], ["direct", "requires_finalizer", "none"])

    def test_python_execution_config_has_no_inline_default_mode_policy(self) -> None:
        source = inspect.getsource(execution.resolve_skill_execution_config)
        self.assertNotIn('"python_subprocess" if manifest.scripts else "delegated_main_agent"', source)
        self.assertNotIn('elif mode == "delegated_main_agent"', source)
        self.assertNotIn('elif mode == "python_subprocess"', source)
        self.assertIn("default_execution_modes", source)
        self.assertIn("default_answer_mode_by_execution_mode", source)

    def test_skill_sandbox_proto_is_owned_by_native_skill_v1(self) -> None:
        proto = Path("native/proto/maf/skill/v1/skill_runtime.proto")
        self.assertTrue(proto.exists())
        text = proto.read_text(encoding="utf-8")
        self.assertIn("package maf.skill.v1;", text)
        self.assertIn("service SkillSandbox", text)
        self.assertIn("rpc ValidatePolicy", text)

    def test_skill_runtime_errors_are_stable_prefixed(self) -> None:
        codes = {entry["code"] for entry in load_skill_runtime_contract()["error_codes"]}
        self.assertIn("skill_runtime_manifest_invalid", codes)
        self.assertIn("skill_runtime_service_not_allowlisted", codes)
        self.assertIn("skill_runtime_sandbox_policy_denied", codes)
        self.assertIn("skill_runtime_artifact_untrusted", codes)
        self.assertIn("skill_runtime_promotion_blocked", codes)
        self.assertIn("skill_runtime_decommission_blocked", codes)
        self.assertTrue(all(code.startswith("skill_runtime_") for code in codes))

    def test_enforce_mode_requires_rust_policy_client_before_platform_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _write_platform_skill(Path(tmp))
            config = resolve_skill_execution_config(manifest)
            registry = SkillPlatformHandlerRegistry(
                handlers={"demo.handler": lambda _ctx: {"response_text": "should not run"}},
                trusted_skill_handlers={"skill.platform": "demo.handler"},
                trusted_skill_services={"skill.platform": ("demo.service",)},
                rust_policy_mode="enforce",
            )

            with self.assertRaises(PermissionError) as context:
                registry.resolve(
                    capability_id="skill.platform",
                    manifest=manifest,
                    config=config,
                    service_registry=SkillServiceRegistry({"demo.service": object()}),
                    public_skill_roots=(Path(tmp),),
                )

        self.assertIn("Rust Skill Runtime policy client is required", str(context.exception))

    def test_enforce_mode_uses_rust_policy_decision_for_platform_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _write_platform_skill(Path(tmp))
            config = resolve_skill_execution_config(manifest)
            client = _FakeRustPolicyClient(allowed=False, code="skill_runtime_service_not_allowlisted")
            registry = SkillPlatformHandlerRegistry(
                handlers={"demo.handler": lambda _ctx: {"response_text": "should not run"}},
                trusted_skill_handlers={"skill.platform": "demo.handler"},
                trusted_skill_services={"skill.platform": ("demo.service",)},
                rust_policy_client=client,
                rust_policy_mode="enforce",
            )

            with self.assertRaises(PermissionError) as context:
                registry.resolve(
                    capability_id="skill.platform",
                    manifest=manifest,
                    config=config,
                    service_registry=SkillServiceRegistry({"demo.service": object()}),
                    public_skill_roots=(Path(tmp),),
                )

        self.assertIn("skill_runtime_service_not_allowlisted", str(context.exception))
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["skill_name"], "platform")
        self.assertEqual(call["capability_id"], "skill.platform")
        self.assertEqual(call["execution_mode"], "platform_service")
        self.assertEqual(call["handler"], "demo.handler")
        self.assertEqual(call["manifest_services"], ("demo.service",))
        self.assertEqual(call["runtime_allowlist_services"], ("demo.service",))
        self.assertEqual(call["requested_services"], ("demo.service",))
        self.assertEqual(call["runtime_allowlist_handlers"], ("demo.handler",))

    def test_shadow_mode_records_rust_policy_diff_without_blocking_python_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _write_platform_skill(Path(tmp))
            config = resolve_skill_execution_config(manifest)
            client = _FakeRustPolicyClient(allowed=False, code="skill_runtime_handler_not_allowlisted")
            audit_events: list[dict[str, str]] = []
            registry = SkillPlatformHandlerRegistry(
                handlers={"demo.handler": lambda _ctx: {"response_text": "handled"}},
                trusted_skill_handlers={"skill.platform": "demo.handler"},
                trusted_skill_services={"skill.platform": ("demo.service",)},
                rust_policy_client=client,
                rust_policy_mode="shadow",
                rust_policy_shadow_diff_sink=audit_events.append,
            )

            handler, services = registry.resolve(
                capability_id="skill.platform",
                manifest=manifest,
                config=config,
                service_registry=SkillServiceRegistry({"demo.service": object()}),
                public_skill_roots=(Path(tmp),),
            )

        self.assertTrue(callable(handler))
        self.assertEqual(sorted(services), ["demo.service"])
        self.assertEqual(len(registry.rust_policy_shadow_diffs), 1)
        diff = registry.rust_policy_shadow_diffs[0]
        self.assertEqual(diff["component"], "maf_skill_runtime")
        self.assertEqual(diff["capability_id"], "skill.platform")
        self.assertEqual(diff["legacy_allowed"], "true")
        self.assertEqual(diff["rust_allowed"], "false")
        self.assertEqual(diff["error_code"], "skill_runtime_handler_not_allowlisted")
        self.assertRegex(diff["input_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertRegex(diff["legacy_output_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertRegex(diff["rust_output_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertGreaterEqual(int(diff["duration_ms"]), 0)
        self.assertEqual(audit_events, [diff])
        self.assertNotIn("hello", str(diff))

    def test_shadow_mode_audits_when_python_legacy_denies_but_rust_would_allow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _write_platform_skill(Path(tmp))
            config = resolve_skill_execution_config(manifest)
            client = _FakeRustPolicyClient(allowed=True)
            audit_events: list[dict[str, str]] = []
            registry = SkillPlatformHandlerRegistry(
                handlers={},
                trusted_skill_handlers={"skill.platform": "demo.handler"},
                trusted_skill_services={"skill.platform": ("demo.service",)},
                rust_policy_client=client,
                rust_policy_mode="shadow",
                rust_policy_shadow_diff_sink=audit_events.append,
            )

            with self.assertRaises(PermissionError):
                registry.resolve(
                    capability_id="skill.platform",
                    manifest=manifest,
                    config=config,
                    service_registry=SkillServiceRegistry({"demo.service": object()}),
                    public_skill_roots=(Path(tmp),),
                )

        self.assertEqual(len(audit_events), 1)
        self.assertEqual(audit_events[0]["legacy_allowed"], "false")
        self.assertEqual(audit_events[0]["rust_allowed"], "true")
        self.assertRegex(audit_events[0]["input_fingerprint"], r"^[0-9a-f]{64}$")

    def test_shadow_audit_sink_failure_does_not_block_legacy_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _write_platform_skill(Path(tmp))
            config = resolve_skill_execution_config(manifest)
            client = _FakeRustPolicyClient(allowed=False, code="skill_runtime_handler_not_allowlisted")

            def failing_sink(_event: Mapping[str, str]) -> None:
                raise OSError("audit sink unavailable")

            registry = SkillPlatformHandlerRegistry(
                handlers={"demo.handler": lambda _ctx: {"response_text": "handled"}},
                trusted_skill_handlers={"skill.platform": "demo.handler"},
                trusted_skill_services={"skill.platform": ("demo.service",)},
                rust_policy_client=client,
                rust_policy_mode="shadow",
                rust_policy_shadow_diff_sink=failing_sink,
            )

            handler, services = registry.resolve(
                capability_id="skill.platform",
                manifest=manifest,
                config=config,
                service_registry=SkillServiceRegistry({"demo.service": object()}),
                public_skill_roots=(Path(tmp),),
            )

        self.assertTrue(callable(handler))
        self.assertEqual(sorted(services), ["demo.service"])
        self.assertEqual(len(registry.rust_policy_shadow_diffs), 1)

class _FakeRustPolicyClient:
    def __init__(self, *, allowed: bool, code: str | None = None) -> None:
        self.allowed = allowed
        self.code = code
        self.calls: list[dict[str, Any]] = []

    def validate_policy(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {
            "allowed": self.allowed,
            "bundle_fingerprint": "fake-fingerprint" if self.allowed else "",
            "error": None
            if self.allowed
            else {
                "code": self.code or "skill_runtime_policy_denied",
                "message": "denied by fake rust policy",
                "retriable": False,
                "category": "security",
                "safe_metadata": {},
            },
        }


def _write_platform_skill(root: Path):
    skill_dir = root / "platform"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\n"
        "name: platform\n"
        "description: platform skill\n"
        "execution:\n"
        "  mode: platform_service\n"
        "  answer_mode: direct\n"
        "  trust_scope: project\n"
        "  handler: demo.handler\n"
        "  services:\n"
        "    - demo.service\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )
    return parse_skill_file(skill_file)


if __name__ == "__main__":
    unittest.main()

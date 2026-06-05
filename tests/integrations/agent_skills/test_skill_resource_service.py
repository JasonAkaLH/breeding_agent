from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.integrations.agent_skills import SkillResourceService, parse_skill_contract_file


class _AuditSink:
    def __init__(self) -> None:
        self.records = []

    def record_sync(self, event_type, payload, **_kwargs):
        self.records.append((event_type, payload))


class SkillResourceServiceTest(unittest.TestCase):
    def _fixture(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        for dirname in ["references", "scripts", "runtime", "schemas"]:
            (root / dirname).mkdir()
        (root / "references" / "usage.md").write_text("Use it. token=abc123 password=secret base_url=https://secret.example", encoding="utf-8")
        (root / "references" / "big.md").write_text("x" * 100, encoding="utf-8")
        (root / "scripts" / "run.py").write_text("print('secret')", encoding="utf-8")
        (root / "runtime" / "handler.py").write_text("HANDLER = 1", encoding="utf-8")
        (root / "schemas" / "input.yaml").write_text("schema_id: x", encoding="utf-8")
        (root / "config.yaml").write_text("password: nope", encoding="utf-8")
        (root / "secret-notes.md").write_text("secret", encoding="utf-8")
        (root / "binary.bin").write_bytes(b"\x00\x01")
        (root / "skill.contract.yaml").write_text(
            """
contract_version: '2'
capability: {id: skill.demo, display_name: Demo}
runtime: {mode: python_subprocess}
entrypoints: {run: {path: scripts/run.py}}
resources:
  usage: {path: references/usage.md, audience: [main_agent, slot_question]}
  big: {path: references/big.md, audience: [main_agent]}
""",
            encoding="utf-8",
        )
        contract = parse_skill_contract_file(root / "skill.contract.yaml")
        return tmp, root, contract

    def test_resource_id_reads_truncates_redacts_and_audits(self) -> None:
        tmp, _root, contract = self._fixture()
        self.addCleanup(tmp.cleanup)
        audit = _AuditSink()
        service = SkillResourceService(audit_sink=audit)
        result = service.read(contract, skill_name="demo", audience="main_agent", resource_id="usage", max_bytes=20)

        self.assertTrue(result.ok)
        self.assertTrue(result.truncated)
        self.assertNotIn("abc123", result.content)
        self.assertEqual(audit.records[-1][0], "skill.resource_read")
        self.assertNotIn("content", audit.records[-1][1])

    def test_path_boundary_and_internal_prompt_denials(self) -> None:
        tmp, _root, contract = self._fixture()
        self.addCleanup(tmp.cleanup)
        service = SkillResourceService()
        self.assertEqual(service.read(contract, skill_name="demo", audience="main_agent", path="../x").denied_reason, "path_denied")
        self.assertEqual(service.read(contract, skill_name="demo", audience="main_agent", path="scripts/run.py").denied_reason, "internal_path_denied")
        self.assertEqual(service.read(contract, skill_name="demo", audience="main_agent", path="schemas/input.yaml").denied_reason, "internal_path_denied")
        self.assertEqual(service.read(contract, skill_name="demo", audience="main_agent", path="config.yaml").denied_reason, "internal_path_denied")

    def test_runtime_can_read_implementation_but_not_secrets(self) -> None:
        tmp, _root, contract = self._fixture()
        self.addCleanup(tmp.cleanup)
        service = SkillResourceService()
        self.assertTrue(service.read(contract, skill_name="demo", audience="runtime", path="runtime/handler.py").ok)
        self.assertEqual(service.read(contract, skill_name="demo", audience="runtime", path="secret-notes.md").denied_reason, "secret_path_denied")

    def test_not_found_binary_and_audience_denied_are_structured(self) -> None:
        tmp, _root, contract = self._fixture()
        self.addCleanup(tmp.cleanup)
        service = SkillResourceService()
        self.assertEqual(service.read(contract, skill_name="demo", audience="main_agent", resource_id="missing").denied_reason, "not_found")
        self.assertEqual(service.read(contract, skill_name="demo", audience="runtime", resource_id="usage").denied_reason, "audience_denied")
        self.assertEqual(service.read(contract, skill_name="demo", audience="main_agent", path="binary.bin").denied_reason, "binary_unsupported")

    def test_symlink_escape_is_denied(self) -> None:
        tmp, root, contract = self._fixture()
        self.addCleanup(tmp.cleanup)
        outside = Path(tempfile.gettempdir()) / "outside-resource-service.txt"
        outside.write_text("outside", encoding="utf-8")
        link = root / "references" / "escape.md"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlink unavailable")
        service = SkillResourceService()
        self.assertEqual(service.read(contract, skill_name="demo", audience="main_agent", path="references/escape.md").denied_reason, "path_denied")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from unittest.mock import patch

from tests.api.support import APITestCase


class CapabilitiesListAPITest(APITestCase):
    def _write_skill(
        self,
        root,
        *,
        name: str = "demo-hot-reload",
        display_name: str = "动态加载 Skill",
        description: str = "动态加载 Skill",
        version: str = "1",
    ) -> None:
        skill_dir = root / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"""---
name: {name}
description: {description}
---

# Demo
请使用动态加载 Skill。
""",
            encoding="utf-8",
        )
        capability_id = "skill." + name.replace("-", "_")
        (skill_dir / "skill.contract.yaml").write_text(
            f"""contract_version: '2'
capability:
  id: {capability_id}
  display_name: {display_name}
  description: {description}
  version: '{version}'
routing:
  triggers:
    - 动态加载
runtime:
  mode: delegated_main_agent
  answer_mode: direct
entrypoints:
  run:
    runtime: delegated_main_agent
""",
            encoding="utf-8",
        )

    def _audit_records(self) -> list[dict]:
        return [
            json.loads(line)
            for line in (self.workspace / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    async def test_capabilities_endpoint_lists_registered_capabilities(self) -> None:
        response = await self.client.get("/api/v1/capabilities")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        capability_ids = {item["capability_id"] for item in payload["capabilities"]}
        self.assertEqual(
            capability_ids,
            {
                "main_agent.respond",
                "skill.generic_data_lookup",
            },
        )
        self.assertNotIn("legacy.query", capability_ids)
        self.assertTrue(all(item["status"] == "active" for item in payload["capabilities"]))

    async def test_capabilities_endpoint_lists_project_skill_capabilities(self) -> None:
        project_skill_root = self.workspace / "skill"
        skill_dir = project_skill_root / "rcbd"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: mini-breedstat-rcbd
description: 生成 RCBD 随机区组设计
triggers:
  - 随机区组
---

# RCBD
""",
            encoding="utf-8",
        )
        (skill_dir / "skill.contract.yaml").write_text(
            """contract_version: '2'
capability:
  id: skill.mini_breedstat_rcbd
  display_name: 田间试验设计
  description: 生成 RCBD 随机区组设计
runtime:
  mode: python_subprocess
  answer_mode: direct
entrypoints:
  run:
    path: scripts/run.py
""",
            encoding="utf-8",
        )
        user_skill_root = self.workspace / "user-skills"
        user_skill_dir = user_skill_root / "private"
        user_skill_dir.mkdir(parents=True)
        (user_skill_dir / "SKILL.md").write_text(
            """---
name: private-helper
description: 私人工具
---

# Private
""",
            encoding="utf-8",
        )
        await self.reconfigure_runtime(
            skill_roots=(project_skill_root, user_skill_root),
            public_skill_roots=(project_skill_root,),
        )

        response = await self.client.get("/api/v1/capabilities")
        self.assertEqual(response.status_code, 200)
        capabilities = {item["capability_id"]: item for item in response.json()["capabilities"]}

        self.assertIn("skill.mini_breedstat_rcbd", capabilities)
        self.assertNotIn("skill.private_helper", capabilities)
        self.assertEqual(capabilities["skill.mini_breedstat_rcbd"]["kind"], "skill")
        self.assertEqual(capabilities["skill.mini_breedstat_rcbd"]["source"], "skill")
        self.assertEqual(capabilities["skill.mini_breedstat_rcbd"]["source_path"], "rcbd/SKILL.md")
        self.assertEqual(capabilities["skill.mini_breedstat_rcbd"]["display_name"], "田间试验设计")

        audit_records = self._audit_records()
        registered = [record for record in audit_records if record["event_type"] == "skill.capability_registered"]
        skipped = [record for record in audit_records if record["event_type"] == "skill.capability_registration_skipped"]
        self.assertEqual(registered[-1]["payload"]["capability_id"], "skill.mini_breedstat_rcbd")
        self.assertEqual(registered[-1]["payload"]["source_path"], "rcbd/SKILL.md")
        self.assertEqual(skipped[-1]["payload"]["reason"], "not_public_scope")

    async def test_capabilities_endpoint_refreshes_added_skill_before_listing(self) -> None:
        project_skill_root = self.workspace / "skill"
        project_skill_root.mkdir(parents=True)
        await self.reconfigure_runtime(skill_roots=(project_skill_root,), public_skill_roots=(project_skill_root,))

        before = await self.client.get("/api/v1/capabilities")
        self.assertEqual(before.status_code, 200)
        self.assertNotIn("skill.demo_hot_reload", {item["capability_id"] for item in before.json()["capabilities"]})

        self._write_skill(project_skill_root)
        after = await self.client.get("/api/v1/capabilities")
        self.assertEqual(after.status_code, 200)
        capabilities = {item["capability_id"]: item for item in after.json()["capabilities"]}
        self.assertIn("skill.demo_hot_reload", capabilities)

        completed = [record for record in self._audit_records() if record["event_type"] == "skill.bundle_refresh_completed"]
        self.assertEqual(completed[-1]["payload"]["reason"], "capabilities_list")

    async def test_capabilities_endpoint_refreshes_updated_skill_metadata_before_listing(self) -> None:
        project_skill_root = self.workspace / "skill"
        self._write_skill(project_skill_root, display_name="旧名称", description="旧描述", version="1")
        await self.reconfigure_runtime(skill_roots=(project_skill_root,), public_skill_roots=(project_skill_root,))

        before = await self.client.get("/api/v1/capabilities")
        self.assertEqual(before.status_code, 200)
        before_capability = {
            item["capability_id"]: item for item in before.json()["capabilities"]
        }["skill.demo_hot_reload"]
        self.assertEqual(before_capability["display_name"], "旧名称")
        self.assertEqual(before_capability["version"], "1")

        previous_revision = self.runtime._skill_runtime_state.active_revision  # noqa: SLF001 - test validates refresh seam
        self._write_skill(project_skill_root, display_name="新名称", description="新描述", version="2")

        after = await self.client.get("/api/v1/capabilities")
        self.assertEqual(after.status_code, 200)
        after_capability = {
            item["capability_id"]: item for item in after.json()["capabilities"]
        }["skill.demo_hot_reload"]
        self.assertEqual(after_capability["display_name"], "新名称")
        self.assertEqual(after_capability["description"], "新描述")
        self.assertEqual(after_capability["version"], "2")
        self.assertNotEqual(self.runtime._skill_runtime_state.active_revision, previous_revision)  # noqa: SLF001

    async def test_capabilities_endpoint_refreshes_deleted_skill_before_listing(self) -> None:
        project_skill_root = self.workspace / "skill"
        self._write_skill(project_skill_root)
        await self.reconfigure_runtime(skill_roots=(project_skill_root,), public_skill_roots=(project_skill_root,))

        before = await self.client.get("/api/v1/capabilities")
        self.assertIn("skill.demo_hot_reload", {item["capability_id"] for item in before.json()["capabilities"]})

        (project_skill_root / "demo-hot-reload" / "SKILL.md").unlink()
        after = await self.client.get("/api/v1/capabilities")
        self.assertEqual(after.status_code, 200)
        self.assertNotIn("skill.demo_hot_reload", {item["capability_id"] for item in after.json()["capabilities"]})

    async def test_capabilities_endpoint_records_skipped_refresh_when_fingerprint_unchanged(self) -> None:
        project_skill_root = self.workspace / "skill"
        self._write_skill(project_skill_root)
        await self.reconfigure_runtime(skill_roots=(project_skill_root,), public_skill_roots=(project_skill_root,))

        first = await self.client.get("/api/v1/capabilities")
        baseline_ids = {item["capability_id"] for item in first.json()["capabilities"]}
        audit_count = len(self._audit_records())

        second = await self.client.get("/api/v1/capabilities")
        self.assertEqual(second.status_code, 200)
        self.assertEqual({item["capability_id"] for item in second.json()["capabilities"]}, baseline_ids)

        new_records = self._audit_records()[audit_count:]
        skipped = [record for record in new_records if record["event_type"] == "skill.bundle_refresh_skipped"]
        self.assertTrue(skipped)
        self.assertEqual(skipped[-1]["payload"]["reason"], "fingerprint_unchanged")

    async def test_capabilities_endpoint_returns_old_list_when_refresh_sync_fails(self) -> None:
        project_skill_root = self.workspace / "skill"
        self._write_skill(project_skill_root, name="baseline-skill", display_name="基础 Skill")
        await self.reconfigure_runtime(skill_roots=(project_skill_root,), public_skill_roots=(project_skill_root,))
        baseline = await self.client.get("/api/v1/capabilities")
        baseline_ids = {item["capability_id"] for item in baseline.json()["capabilities"]}
        baseline_revision = self.runtime._skill_runtime_state.active_revision  # noqa: SLF001 - test validates rollback seam
        audit_count = len(self._audit_records())

        self._write_skill(project_skill_root, name="new-skill", display_name="新增 Skill")
        with patch.object(self.runtime, "_sync_skill_capability_registry", side_effect=RuntimeError("boom")):
            response = await self.client.get("/api/v1/capabilities")

        self.assertEqual(response.status_code, 200)
        self.assertEqual({item["capability_id"] for item in response.json()["capabilities"]}, baseline_ids)
        self.assertNotIn("skill.new_skill", {item["capability_id"] for item in response.json()["capabilities"]})
        self.assertEqual(self.runtime._skill_runtime_state.active_revision, baseline_revision)  # noqa: SLF001

        new_records = self._audit_records()[audit_count:]
        failed = [record for record in new_records if record["event_type"] == "skill.bundle_refresh_failed"]
        self.assertTrue(failed)
        self.assertEqual(failed[-1]["payload"]["reason"], "capabilities_list")
        self.assertEqual(failed[-1]["payload"]["fallback_revision"], baseline_revision)
        self.assertEqual(failed[-1]["payload"]["error_type"], "RuntimeError")

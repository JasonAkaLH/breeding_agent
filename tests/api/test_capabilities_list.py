from __future__ import annotations

import json

from tests.api.support import APITestCase


class CapabilitiesListAPITest(APITestCase):
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

        audit_records = [
            json.loads(line)
            for line in (self.workspace / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        registered = [record for record in audit_records if record["event_type"] == "skill.capability_registered"]
        skipped = [record for record in audit_records if record["event_type"] == "skill.capability_registration_skipped"]
        self.assertEqual(registered[-1]["payload"]["capability_id"], "skill.mini_breedstat_rcbd")
        self.assertEqual(registered[-1]["payload"]["source_path"], "rcbd/SKILL.md")
        self.assertEqual(skipped[-1]["payload"]["reason"], "not_public_scope")

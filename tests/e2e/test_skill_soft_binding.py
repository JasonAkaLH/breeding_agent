from __future__ import annotations

import json

from src.orchestration.agent_loop.models import (
    AgentItemKind,
    AgentItemState,
)
from src.storage.agent_payload import canonicalize_agent_payload
from tests.e2e.support import E2EAPITestCase


class SkillSoftBindingE2ETest(E2EAPITestCase):
    def _write_bioinfo_skill(self):
        root = self.workspace / "soft-binding-skills"
        skill = root / "bioinfo-daily"
        (skill / "scripts").mkdir(parents=True)
        (skill / "schemas").mkdir()
        marker = self.workspace / "bioinfo-network-fake-count.txt"
        (skill / "SKILL.md").write_text(
            """---
name: bioinfo-daily
description: 按日期范围检索育种文献并提供完整 JSON 结果
---

INTERNAL_INSTRUCTION_MUST_NOT_APPEAR_BEFORE_CALL
""",
            encoding="utf-8",
        )
        (skill / "skill.contract.yaml").write_text(
            """contract_version: '2'
capability:
  id: skill.bioinfo_daily
  display_name: Bioinfo Daily
  description: 按日期范围检索育种文献并提供完整 JSON 结果
runtime: {mode: python_subprocess, answer_mode: direct}
entrypoints:
  run:
    path: scripts/run.py
    input_schema: search
    output: literature
input_schemas:
  search:
    path: schemas/search.input.yaml
    title: 文献检索
    description: 可指定日期范围和最大结果数
outputs:
  literature:
    required: [answer, articles]
    artifacts:
      - extensions: [.json]
        mime_types: [application/json]
""",
            encoding="utf-8",
        )
        (skill / "schemas" / "search.input.yaml").write_text(
            """schema_id: search
inputs:
  date_range:
    type: string
    title: 日期范围
    description: 要检索的日期范围
    default: 最近七天
  max_results:
    type: integer
    title: 最大结果数
    description: 最多返回多少篇文献
    default: 30
    validation: {min: 1, max: 100}
""",
            encoding="utf-8",
        )
        (skill / "scripts" / "run.py").write_text(
            f"""import json
from pathlib import Path
marker = Path({str(marker)!r})
count = int(marker.read_text(encoding='utf-8')) + 1 if marker.exists() else 1
marker.write_text(str(count), encoding='utf-8')
articles = [
    {{
        'title': f'article-{{index}}',
        'abstract': 'breeding research ' * 700,
        'url': f'https://example.test/articles/{{index}}',
    }}
    for index in range(28)
]
print(json.dumps({{
    'answer': 'search complete',
    'search_summary': 'found 28 articles',
    'articles': articles,
    'structured_content': {{'articles': articles}},
}}, ensure_ascii=False))
""",
            encoding="utf-8",
        )
        return root, marker

    async def test_hint_answers_from_profile_or_executes_once_with_bounded_result(self) -> None:
        skill_root, network_marker = self._write_bioinfo_skill()
        prompts: list[str] = []

        def agent_fixture(prompt: str, **_kwargs) -> str:
            prompts.append(prompt)
            if '"outcome":"completed"' in prompt:
                return "检索已完成；完整 28 篇结果可从 Artifact 下载。"
            if "检索最近七天的育种文献" in prompt:
                return json.dumps(
                    {
                        "tool_calls": [
                            {
                                "capability_id": "skill.bioinfo_daily",
                                "arguments": {
                                    "date_range": "最近七天",
                                    "max_results": 30,
                                },
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            return "这个 Skill 可按日期范围检索育种文献，默认 30 篇、上限 100，并提供 JSON 结果。"

        await self.reconfigure_runtime(
            skill_roots=(skill_root,),
            public_skill_roots=(skill_root,),
            main_agent_stream_generator=agent_fixture,
            enable_conversation_memory=False,
        )

        informational = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-bioinfo-informational",
                "content": "你看看这个 Skill 是干什么的",
                "routing_mode": "hint",
                "capability_id": "skill.bioinfo_daily",
                "metadata": {},
            },
        )
        self.assertEqual(informational.status_code, 202, informational.text)
        informational_task_id = informational.json()["task_id"]
        self.assertEqual(
            (await self.wait_for_terminal_task(informational_task_id))["status"],
            "completed",
        )
        self.assertFalse(network_marker.exists())
        self.assertIn('"name":"date_range"', prompts[0])
        self.assertIn('"default":30', prompts[0])
        self.assertIn('"max":100.0', prompts[0])
        self.assertIn('"output_contracts"', prompts[0])
        self.assertNotIn("INTERNAL_INSTRUCTION_MUST_NOT_APPEAR_BEFORE_CALL", prompts[0])
        informational_run = await self.runtime.agent_run_repository.get_run_for_task(
            informational_task_id
        )
        assert informational_run is not None
        informational_items = await self.runtime.agent_run_repository.list_items(
            informational_run.run_id
        )
        self.assertEqual(
            sum(item.kind is AgentItemKind.SKILL_ACTIVATION for item in informational_items),
            1,
        )
        self.assertEqual(
            sum(item.kind is AgentItemKind.TOOL_CALL for item in informational_items),
            0,
        )
        self.assertEqual(
            [node.capability_id for node in await self.runtime.storage.list_task_nodes_for_task(informational_task_id)],
            ["agent.final_output"],
        )

        execution = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-bioinfo-execution",
                "content": "检索最近七天的育种文献",
                "routing_mode": "hint",
                "capability_id": "skill.bioinfo_daily",
                "metadata": {},
            },
        )
        self.assertEqual(execution.status_code, 202, execution.text)
        execution_task_id = execution.json()["task_id"]
        self.assertEqual(
            (await self.wait_for_terminal_task(execution_task_id))["status"],
            "completed",
        )
        self.assertEqual(network_marker.read_text(encoding="utf-8"), "1")
        execution_run = await self.runtime.agent_run_repository.get_run_for_task(
            execution_task_id
        )
        assert execution_run is not None
        execution_items = await self.runtime.agent_run_repository.list_items(
            execution_run.run_id
        )
        self.assertEqual(
            sum(item.kind is AgentItemKind.SKILL_ACTIVATION for item in execution_items),
            1,
        )
        self.assertEqual(
            sum(item.kind is AgentItemKind.TOOL_CALL for item in execution_items),
            1,
        )
        result_item = next(
            item
            for item in execution_items
            if item.kind is AgentItemKind.TOOL_RESULT
            and item.state is AgentItemState.COMMITTED
        )
        result_payload = json.loads(result_item.payload_json)
        safe_result = result_payload["safe_result"]
        self.assertEqual(safe_result["projection_mode"], "artifact_backed")
        self.assertLessEqual(canonicalize_agent_payload(safe_result).size_bytes, 80_000)
        self.assertNotIn("article-0", json.dumps(safe_result))
        self.assertFalse(
            any(item.state is AgentItemState.RESERVED for item in execution_items)
        )

        artifacts = (
            await self.client.get(f"/api/v1/tasks/{execution_task_id}/artifacts")
        ).json()["artifacts"]
        result_artifacts = [
            artifact for artifact in artifacts if artifact["filename"] == "skill_result.json"
        ]
        self.assertEqual(len(result_artifacts), 1)
        raw_download = await self.client.get(result_artifacts[0]["download_url"])
        self.assertEqual(raw_download.status_code, 200)
        raw = raw_download.json()
        self.assertEqual(len(raw["articles"]), 28)
        self.assertEqual(raw["articles"], raw["structured_content"]["articles"])
        history = await self.client.get(
            "/api/v1/conversations/conv-bioinfo-execution/messages"
        )
        assistant = next(
            message for message in history.json()["messages"] if message["role"] == "assistant"
        )
        self.assertIn("Artifact", assistant["content"])
        events = await self.runtime.storage.list_events_for_task(execution_task_id)
        self.assertEqual(
            sum(event.event_type == "agent.result_projected" for event in events),
            1,
        )


if __name__ == "__main__":
    import unittest

    unittest.main()

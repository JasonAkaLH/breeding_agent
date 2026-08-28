from __future__ import annotations

import hashlib
import json

from src.orchestration.agent_loop.models import (
    AgentItemKind,
    AgentItemState,
)
from tests.e2e.support import E2EAPITestCase


class SkillSoftBindingE2ETest(E2EAPITestCase):
    def _write_bioinfo_skill(self, *, answer_mode: str = "requires_finalizer"):
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
            f"""contract_version: '2'
capability:
  id: skill.bioinfo_daily
  display_name: Bioinfo Daily
  description: 按日期范围检索育种文献并提供完整 JSON 结果
runtime: {{mode: python_subprocess, answer_mode: {answer_mode}}}
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
        'title': f'article-{{index + 1}}',
        'abstract': 'breeding research ' * 700,
        'url': f'https://example.test/articles/{{index}}',
        'sentinel': (
            'FIRST-ARTICLE-SENTINEL' if index == 0
            else 'UNIQUE-ARTICLE-28-SENTINEL' if index == 27
            else f'article-sentinel-{{index + 1}}'
        ),
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

    async def test_hint_executes_once_and_model_receives_all_28_transient_records(self) -> None:
        skill_root, network_marker = self._write_bioinfo_skill()
        prompts: list[str] = []

        def agent_fixture(prompt: str, **_kwargs) -> str:
            prompts.append(prompt)
            if "maf.agent.skill_result_full.v1" in prompt:
                return "检索已完成；第28条标记是 UNIQUE-ARTICLE-28-SENTINEL。"
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
        model_requests = []
        model = self.runtime.agent_loop_orchestrator._runner._model  # noqa: SLF001
        original_sample = model.sample_agent

        async def capture_model_request(request):
            model_requests.append(request)
            return await original_sample(request)

        model.sample_agent = capture_model_request

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
        self.assertEqual(safe_result["projection_mode"], "transient_staged")
        self.assertEqual(safe_result["projection_revision"], "skill-result-v2")
        self.assertNotIn("article-0", json.dumps(safe_result))
        self.assertEqual(result_payload["artifact_refs"], [])
        self.assertFalse(
            any(item.state is AgentItemState.RESERVED for item in execution_items)
        )

        artifacts = (
            await self.client.get(f"/api/v1/tasks/{execution_task_id}/artifacts")
        ).json()["artifacts"]
        self.assertFalse(
            any(artifact["filename"] == "skill_result.json" for artifact in artifacts)
        )
        resolved_requests = [
            request
            for request in model_requests
            if any(
                message.role == "tool"
                and "maf.agent.skill_result_full.v1" in (message.content or "")
                for message in request.messages
            )
        ]
        self.assertEqual(len(resolved_requests), 1)
        tool_content = next(
            message.content
            for message in resolved_requests[0].messages
            if message.role == "tool"
        )
        tool_payload = json.loads(tool_content)
        raw = tool_payload["safe_result"]["result"]
        self.assertEqual(len(raw["articles"]), 28)
        self.assertEqual(raw["articles"], raw["structured_content"]["articles"])
        self.assertEqual(raw["articles"][0]["sentinel"], "FIRST-ARTICLE-SENTINEL")
        self.assertEqual(
            raw["articles"][27]["sentinel"],
            "UNIQUE-ARTICLE-28-SENTINEL",
        )
        raw_bytes = (
            json.dumps(
                raw,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(raw_bytes).hexdigest(), safe_result["raw_sha256"]
        )
        self.assertNotIn("stage_ref", tool_content)
        self.assertNotIn("pending_context_injection", tool_content)
        self.assertFalse(
            any(
                request.request_id.startswith("agent-compaction:")
                for request in model_requests
            )
        )
        self.assertEqual(
            tuple(
                path
                for path in (
                    self.workspace / "agent_transient_skill_results"
                ).rglob("*")
                if path.is_file()
            ),
            (),
        )
        history = await self.client.get(
            "/api/v1/conversations/conv-bioinfo-execution/messages"
        )
        assistant = next(
            message for message in history.json()["messages"] if message["role"] == "assistant"
        )
        self.assertIn("UNIQUE-ARTICLE-28-SENTINEL", assistant["content"])
        events = await self.runtime.storage.list_events_for_task(execution_task_id)
        self.assertEqual(
            sum(event.event_type == "agent.result_projected" for event in events),
            1,
        )

    async def test_same_large_result_with_business_artifact_stays_legacy(self) -> None:
        skill_root, network_marker = self._write_bioinfo_skill(answer_mode="direct")

        def agent_fixture(prompt: str, **_kwargs) -> str:
            if '"outcome":"completed"' in prompt:
                return "检索已完成，请查看现有 Artifact。"
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

        await self.reconfigure_runtime(
            skill_roots=(skill_root,),
            public_skill_roots=(skill_root,),
            main_agent_stream_generator=agent_fixture,
            enable_conversation_memory=False,
        )
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-bioinfo-legacy-artifact",
                "content": "检索最近七天的育种文献",
                "routing_mode": "hint",
                "capability_id": "skill.bioinfo_daily",
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        self.assertEqual(
            (await self.wait_for_terminal_task(task_id))["status"],
            "completed",
        )
        self.assertEqual(network_marker.read_text(encoding="utf-8"), "1")

        run = await self.runtime.agent_run_repository.get_run_for_task(task_id)
        assert run is not None
        items = await self.runtime.agent_run_repository.list_items(run.run_id)
        result_item = next(
            item
            for item in items
            if item.kind is AgentItemKind.TOOL_RESULT
            and item.state is AgentItemState.COMMITTED
        )
        result_payload = json.loads(result_item.payload_json)
        safe_result = result_payload["safe_result"]
        self.assertEqual(safe_result["projection_revision"], "skill-result-v1")
        self.assertEqual(safe_result["projection_mode"], "artifact_backed")
        self.assertGreaterEqual(len(result_payload["artifact_refs"]), 2)
        self.assertEqual(
            tuple(
                (self.workspace / "agent_transient_skill_results" / "raw").iterdir()
            ),
            (),
        )

        artifacts = (
            await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")
        ).json()["artifacts"]
        result_artifact = next(
            artifact
            for artifact in artifacts
            if artifact["filename"] == "skill_result.json"
        )
        downloaded = await self.client.get(result_artifact["download_url"])
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(len(downloaded.json()["articles"]), 28)

if __name__ == "__main__":
    import unittest

    unittest.main()

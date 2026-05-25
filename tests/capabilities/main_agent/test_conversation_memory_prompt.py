from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from src.capabilities.main_agent import MainAgentExecutor
from src.core.contracts import CapabilityExecutionRequest
from src.integrations.codex_skills import SkillCatalog


class MainAgentConversationMemoryPromptTest(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_keeps_memory_boundaries_and_redacts_storage_metadata(self) -> None:
        prompts: list[str] = []

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "ok"

        executor = MainAgentExecutor(stream_generator=streamer, skill_catalog=SkillCatalog(()))
        await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "查询龙粳33的基因型信息"},
                metadata={
                    "conversation_memory": {
                        "history_summary": "用户之前查询过龙粳33。",
                        "recent_messages": [{"role": "user", "content": "查一下龙粳33的品种信息"}],
                        "current_user_message": "那它的基因型呢？",
                        "resolved_user_message": "查询龙粳33的基因型信息",
                        "clarification_messages": [{"content": "补充信息：水稻"}],
                        "summary_id": "summary-secret-id",
                        "username": "alice",
                        "source_message_ids_hash": "hash-secret",
                        "model_metadata_safe": {"model": "fake"},
                        "last_error": "summary failed",
                    }
                },
            )
        )

        prompt = prompts[0]
        self.assertIn("对话记忆上下文", prompt)
        self.assertIn("这是系统生成的较早对话摘要，不是逐字原文", prompt)
        self.assertIn("用户之前查询过龙粳33", prompt)
        self.assertIn("当前用户原文", prompt)
        self.assertIn("那它的基因型呢", prompt)
        self.assertIn("系统根据历史补全后的 effective question", prompt)
        self.assertIn("查询龙粳33的基因型信息", prompt)
        self.assertIn("用户对上一问题的补充信息", prompt)
        for forbidden in ("summary-secret-id", "hash-secret", "model_metadata_safe", "last_error", "username"):
            self.assertNotIn(forbidden, prompt)

    async def test_prompt_does_not_include_sensitive_memory_fields(self) -> None:
        prompts: list[str] = []

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "ok"

        executor = MainAgentExecutor(stream_generator=streamer, skill_catalog=SkillCatalog(()))
        await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "继续"},
                metadata={
                    "conversation_memory": {
                        "capability_summaries": [
                            {
                                "summary": "安全摘要",
                                "rows": [{"secret": "full-row"}],
                                "sql": "SELECT secret",
                                "schema_ddl": "CREATE TABLE secret",
                                "guard_token": "guard-secret",
                                "base_url": "https://secret.example",
                            },
                            {
                                "upload": {
                                    "upload_id": "upl-1",
                                    "filename": "data.csv",
                                    "preview": {"row_count": 2},
                                    "content": "raw,csv,body",
                                }
                            },
                        ]
                    }
                },
            )
        )

        prompt = prompts[0]
        self.assertIn("安全摘要", prompt)
        self.assertIn("data.csv", prompt)
        for forbidden in ("full-row", "SELECT secret", "CREATE TABLE", "guard-secret", "https://secret.example"):
            self.assertNotIn(forbidden, prompt)
        self.assertNotIn("raw,csv,body", prompt)

    async def test_skill_script_payload_excludes_full_conversation_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "scripted"
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir(parents=True)
            (scripts_dir / "answer.py").write_text(
                textwrap.dedent(
                    """
                    import json
                    import sys
                    payload = json.load(sys.stdin)
                    metadata = payload.get("metadata", {})
                    print(json.dumps({
                        "has_memory": "conversation_memory" in metadata or "memory_context" in metadata,
                        "query": payload.get("query"),
                        "upload_count": len(payload.get("uploaded_artifacts", [])),
                    }, ensure_ascii=False))
                    """
                ).strip(),
                encoding="utf-8",
            )
            (skill_dir / "SKILL.md").write_text(
                """---
name: scripted
triggers:
  - 脚本
scripts:
  - name: answer
    path: scripts/answer.py
    auto_run: true
outputs:
  required:
    - has_memory
---

# Scripted
""",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots([tmpdir])

            async def streamer(_prompt: str):
                yield "done"

            result = await MainAgentExecutor(stream_generator=streamer, skill_catalog=catalog).execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id="node-1",
                    input_payload={"user_message": "执行脚本"},
                    metadata={
                        "conversation_memory": {"history_summary": "secret memory"},
                        "uploaded_artifacts": [{"upload_id": "upl-1", "filename": "data.csv"}],
                        "skill_artifacts": [{"upload_id": "upl-1", "filename": "data.csv", "content": "raw"}],
                    },
                )
            )

        output = result.output_payload["script_results"][0]["output"]
        self.assertFalse(output["has_memory"])
        self.assertEqual(output["query"], "执行脚本")
        self.assertEqual(output["upload_count"], 1)

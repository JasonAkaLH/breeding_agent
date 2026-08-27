from __future__ import annotations

import io
import zipfile
from pathlib import Path

from tests.api.support import APITestCase


class SkillOutputArtifactsAPITest(APITestCase):
    @staticmethod
    def _main_agent_llm_config() -> dict:
        return {
            "api_key": "test-key",
            "model": "test-model",
            "model_editions": {
                "default": "test-model",
                "options": [
                    {
                        "value": "test-model",
                        "label": "Test Model",
                        "reasoning_efforts": {
                            "default": "minimal",
                            "disabled_default": "minimal",
                            "options": [
                                {"value": "minimal", "label": "Minimal", "allow_when_thinking_disabled": True},
                            ],
                        },
                        "agent_capabilities": {
                            "supports_messages": True,
                            "roles": ["system", "user", "assistant", "tool"],
                            "supports_native_tools": True,
                            "supports_required_tool_choice": True,
                            "supports_streamed_tool_calls": True,
                        },
                    }
                ],
            },
        }

    def _write_skill(self, *, script_body: str) -> Path:
        root = self.workspace / "skills" / "file_skill"
        scripts = root / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (root / "SKILL.md").write_text(
            """---
name: file-skill
description: 文件产出测试 Skill
triggers: [生成文件, 多文件, 压缩, 拒绝压缩]
---
用于测试输出文件。
""",
            encoding="utf-8",
        )
        (root / "skill.contract.yaml").write_text(
            """contract_version: '2'
capability: {id: skill.file_output, display_name: File Output}
runtime: {mode: python_subprocess, answer_mode: direct}
entrypoints: {emit: {path: scripts/emit.py}}
""",
            encoding="utf-8",
        )
        (scripts / "emit.py").write_text(script_body, encoding="utf-8")
        return root.parent

    async def _use_skill(self, script_body: str) -> None:
        roots = self._write_skill(script_body=script_body)
        await self.reconfigure_runtime(
            skill_roots=(roots,),
            main_agent_stream_generator=lambda prompt: "主代理回答：文件已处理。",
            main_agent_llm_config=self._main_agent_llm_config(),
            enable_conversation_memory=False,
        )
        self.active_skill_id = "skill.file_output"

    async def _use_multi_script_skill(self) -> None:
        root = self.workspace / "skills" / "multi_script_skill"
        scripts = root / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (root / "SKILL.md").write_text(
            """---
name: multi-script-file-skill
description: 多脚本文件产出测试 Skill
triggers: [生成文件]
---
用于测试同一响应内多个脚本输出文件。
""",
            encoding="utf-8",
        )
        (scripts / "first.py").write_text(
            """import json, os
from pathlib import Path
out = Path(os.environ['MAF_SKILL_OUTPUT_DIR'])
out.mkdir(parents=True, exist_ok=True)
(out / 'first.txt').write_text('first', encoding='utf-8')
print(json.dumps({'answer': 'first', 'output_files': [{'path': 'outputs/first.txt'}]}, ensure_ascii=False))
""",
            encoding="utf-8",
        )
        (scripts / "second.py").write_text(
            """import json, os
from pathlib import Path
out = Path(os.environ['MAF_SKILL_OUTPUT_DIR'])
out.mkdir(parents=True, exist_ok=True)
(out / 'second.txt').write_text('second', encoding='utf-8')
print(json.dumps({'answer': 'second', 'output_files': [{'path': 'outputs/second.txt'}]}, ensure_ascii=False))
""",
            encoding="utf-8",
        )
        await self.reconfigure_runtime(
            skill_roots=(root.parent,),
            main_agent_stream_generator=lambda prompt: "主代理回答：文件已处理。",
            main_agent_llm_config=self._main_agent_llm_config(),
            enable_conversation_memory=False,
        )
        self.active_skill_id = "skill.multi_script_file_skill"

    async def test_skill_output_file_is_downloadable_without_exposing_storage_ref(self) -> None:
        await self._use_skill(
            """import json, os
from pathlib import Path
out = Path(os.environ['MAF_SKILL_OUTPUT_DIR'])
out.mkdir(parents=True, exist_ok=True)
(out / 'layout.html').write_text('<h1>layout</h1>', encoding='utf-8')
print(json.dumps({'answer': 'ok', 'output_files': [{'path': 'outputs/layout.html', 'filename': 'layout.html', 'mime_type': 'text/html', 'label': '布局', 'summary': 'HTML 布局'}]}, ensure_ascii=False))
"""
        )
        response = await self.submit_message(conversation_id="conv-file", content="请生成文件", capability_id=self.active_skill_id)
        task_id = response.json()["task_id"]
        await self.wait_for_terminal_task(task_id)

        artifacts_response = await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")
        artifacts_response.raise_for_status()
        artifacts = artifacts_response.json()["artifacts"]
        file_artifact = next(artifact for artifact in artifacts if artifact["artifact_type"] == "file")

        self.assertEqual(file_artifact["filename"], "layout.html")
        self.assertEqual(file_artifact["mime_type"], "text/html")
        self.assertEqual(file_artifact["storage_ref"], "")
        self.assertEqual(file_artifact["download_url"], f"/api/v1/artifacts/{file_artifact['artifact_id']}/download")
        self.assertEqual(file_artifact["source_file_count"], 1)
        self.assertIsNone(file_artifact["archive_format"])

        download = await self.client.get(file_artifact["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers["x-content-type-options"], "nosniff")
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(download.text, "<h1>layout</h1>")

    async def test_new_output_replaces_old_output_in_same_conversation(self) -> None:
        await self._use_skill(
            """import json, os
from pathlib import Path
payload = json.load(__import__('sys').stdin)
text = payload.get('query', '')
out = Path(os.environ['MAF_SKILL_OUTPUT_DIR'])
out.mkdir(parents=True, exist_ok=True)
(out / 'result.txt').write_text(text, encoding='utf-8')
print(json.dumps({'answer': 'ok', 'output_files': [{'path': 'outputs/result.txt', 'filename': 'result.txt', 'mime_type': 'text/plain'}]}, ensure_ascii=False))
"""
        )
        first = await self.submit_message(conversation_id="conv-replace", content="请生成文件 第一版", capability_id=self.active_skill_id)
        first_task_id = first.json()["task_id"]
        await self.wait_for_terminal_task(first_task_id)
        first_artifacts = (await self.client.get(f"/api/v1/tasks/{first_task_id}/artifacts")).json()["artifacts"]
        first_file = next(artifact for artifact in first_artifacts if artifact["artifact_type"] == "file")

        second = await self.submit_message(conversation_id="conv-replace", content="请生成文件 第二版", capability_id=self.active_skill_id)
        second_task_id = second.json()["task_id"]
        await self.wait_for_terminal_task(second_task_id)
        second_artifacts = (await self.client.get(f"/api/v1/tasks/{second_task_id}/artifacts")).json()["artifacts"]
        second_file = next(artifact for artifact in second_artifacts if artifact["artifact_type"] == "file")

        old_download = await self.client.get(first_file["download_url"])
        self.assertEqual(old_download.status_code, 404)
        new_download = await self.client.get(second_file["download_url"])
        self.assertEqual(new_download.status_code, 200)
        self.assertIn("第二版", new_download.text)
        old_task_artifacts = (await self.client.get(f"/api/v1/tasks/{first_task_id}/artifacts")).json()["artifacts"]
        self.assertFalse(any(artifact["artifact_type"] == "file" for artifact in old_task_artifacts))

    async def test_multiple_outputs_are_downloaded_as_single_platform_zip(self) -> None:
        await self._use_skill(
            """import json, os
from pathlib import Path
out = Path(os.environ['MAF_SKILL_OUTPUT_DIR'])
out.mkdir(parents=True, exist_ok=True)
(out / 'layout.html').write_text('<h1>layout</h1>', encoding='utf-8')
(out / 'fieldbook.csv').write_text('plot,entry\\n1,A\\n', encoding='utf-8')
print(json.dumps({'answer': 'ok', 'output_files': [{'path': 'outputs/layout.html'}, {'path': 'outputs/fieldbook.csv'}]}, ensure_ascii=False))
"""
        )
        response = await self.submit_message(conversation_id="conv-zip", content="请生成多文件", capability_id=self.active_skill_id)
        task_id = response.json()["task_id"]
        await self.wait_for_terminal_task(task_id)
        artifacts = (await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")).json()["artifacts"]
        file_artifact = next(artifact for artifact in artifacts if artifact["artifact_type"] == "file")

        self.assertEqual(file_artifact["mime_type"], "application/zip")
        self.assertEqual(file_artifact["archive_format"], "zip")
        self.assertEqual(file_artifact["source_file_count"], 2)
        download = await self.client.get(file_artifact["download_url"])
        self.assertEqual(download.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["fieldbook.csv", "layout.html"])
            self.assertEqual(archive.read("layout.html").decode("utf-8"), "<h1>layout</h1>")

    async def test_cross_account_download_is_not_found(self) -> None:
        await self._use_skill(
            """import json, os
from pathlib import Path
out = Path(os.environ['MAF_SKILL_OUTPUT_DIR'])
out.mkdir(parents=True, exist_ok=True)
(out / 'result.txt').write_text('secret', encoding='utf-8')
print(json.dumps({'answer': 'ok', 'output_files': [{'path': 'outputs/result.txt'}]}, ensure_ascii=False))
"""
        )
        response = await self.submit_message(conversation_id="conv-owner", content="请生成文件", capability_id=self.active_skill_id)
        task_id = response.json()["task_id"]
        await self.wait_for_terminal_task(task_id)
        artifact = next(
            artifact
            for artifact in (await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")).json()["artifacts"]
            if artifact["artifact_type"] == "file"
        )

        await self.login("acc-2")
        denied = await self.client.get(artifact["download_url"])
        self.assertEqual(denied.status_code, 404)

    async def test_direct_source_zip_is_rejected_without_failing_task(self) -> None:
        await self._use_skill(
            """import json, os
from pathlib import Path
out = Path(os.environ['MAF_SKILL_OUTPUT_DIR'])
out.mkdir(parents=True, exist_ok=True)
(out / 'bad.zip').write_bytes(b'not trusted')
print(json.dumps({'answer': 'ok', 'output_files': [{'path': 'outputs/bad.zip'}]}, ensure_ascii=False))
"""
        )
        response = await self.submit_message(conversation_id="conv-source-zip", content="拒绝压缩", capability_id=self.active_skill_id)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        artifacts = (await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")).json()["artifacts"]
        self.assertFalse(any(artifact["artifact_type"] == "file" for artifact in artifacts))

    async def test_file_store_failure_does_not_fail_main_agent_task(self) -> None:
        await self._use_skill(
            """import json, os
from pathlib import Path
out = Path(os.environ['MAF_SKILL_OUTPUT_DIR'])
out.mkdir(parents=True, exist_ok=True)
(out / 'result.txt').write_text('content', encoding='utf-8')
print(json.dumps({'answer': 'ok', 'output_files': [{'path': 'outputs/result.txt'}]}, ensure_ascii=False))
"""
        )

        def fail_save_file(**kwargs):
            raise OSError("disk full")

        self.runtime.artifact_file_store.save_file = fail_save_file  # type: ignore[method-assign]
        response = await self.submit_message(conversation_id="conv-store-fail", content="请生成文件", capability_id=self.active_skill_id)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        artifacts = (await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")).json()["artifacts"]
        self.assertFalse(any(artifact["artifact_type"] == "file" for artifact in artifacts))
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertTrue(any(event.event_type == "skill.output_file_rejected" for event in events))

    async def test_old_file_delete_failure_keeps_new_artifact_and_hides_old_download(self) -> None:
        await self._use_skill(
            """import json, os
from pathlib import Path
payload = json.load(__import__('sys').stdin)
text = payload.get('query', '')
out = Path(os.environ['MAF_SKILL_OUTPUT_DIR'])
out.mkdir(parents=True, exist_ok=True)
(out / 'result.txt').write_text(text, encoding='utf-8')
print(json.dumps({'answer': 'ok', 'output_files': [{'path': 'outputs/result.txt'}]}, ensure_ascii=False))
"""
        )
        first = await self.submit_message(conversation_id="conv-evict-fail", content="请生成文件 第一版", capability_id=self.active_skill_id)
        first_task_id = first.json()["task_id"]
        await self.wait_for_terminal_task(first_task_id)
        first_file = next(
            artifact
            for artifact in (await self.client.get(f"/api/v1/tasks/{first_task_id}/artifacts")).json()["artifacts"]
            if artifact["artifact_type"] == "file"
        )

        def fail_delete(storage_key: str) -> bool:
            raise OSError("locked")

        self.runtime.artifact_file_store.delete = fail_delete  # type: ignore[method-assign]
        second = await self.submit_message(conversation_id="conv-evict-fail", content="请生成文件 第二版", capability_id=self.active_skill_id)
        second_task_id = second.json()["task_id"]
        terminal = await self.wait_for_terminal_task(second_task_id)
        self.assertEqual(terminal["status"], "completed")
        second_artifacts = (await self.client.get(f"/api/v1/tasks/{second_task_id}/artifacts")).json()["artifacts"]
        second_file = next(artifact for artifact in second_artifacts if artifact["artifact_type"] == "file")
        first_download = await self.client.get(first_file["download_url"])
        self.assertEqual(first_download.status_code, 404)
        second_download = await self.client.get(second_file["download_url"])
        self.assertEqual(second_download.status_code, 200)
        self.assertIn("第二版", second_download.text)

    async def test_old_metadata_supersede_failure_rejects_new_output_without_failing_task(self) -> None:
        await self._use_skill(
            """import json, os
from pathlib import Path
payload = json.load(__import__('sys').stdin)
text = payload.get('query', '')
out = Path(os.environ['MAF_SKILL_OUTPUT_DIR'])
out.mkdir(parents=True, exist_ok=True)
(out / 'result.txt').write_text(text, encoding='utf-8')
print(json.dumps({'answer': 'ok', 'output_files': [{'path': 'outputs/result.txt'}]}, ensure_ascii=False))
"""
        )
        first = await self.submit_message(conversation_id="conv-evict-metadata-fail", content="请生成文件 第一版", capability_id=self.active_skill_id)
        first_task_id = first.json()["task_id"]
        await self.wait_for_terminal_task(first_task_id)
        first_file = next(
            artifact
            for artifact in (await self.client.get(f"/api/v1/tasks/{first_task_id}/artifacts")).json()["artifacts"]
            if artifact["artifact_type"] == "file"
        )
        original_save_artifact = self.runtime.storage.save_artifact

        async def fail_old_metadata_save(artifact):
            if artifact.artifact_id == first_file["artifact_id"]:
                raise RuntimeError("db locked")
            return await original_save_artifact(artifact)

        self.runtime.storage.save_artifact = fail_old_metadata_save  # type: ignore[method-assign]
        second = await self.submit_message(conversation_id="conv-evict-metadata-fail", content="请生成文件 第二版", capability_id=self.active_skill_id)
        second_task_id = second.json()["task_id"]
        terminal = await self.wait_for_terminal_task(second_task_id)
        self.assertEqual(terminal["status"], "completed")
        second_artifacts = (await self.client.get(f"/api/v1/tasks/{second_task_id}/artifacts")).json()["artifacts"]
        self.assertFalse(any(artifact["artifact_type"] == "file" for artifact in second_artifacts))
        first_download = await self.client.get(first_file["download_url"])
        self.assertEqual(first_download.status_code, 200)
        self.assertIn("第一版", first_download.text)
        events = await self.runtime.storage.list_events_for_task(second_task_id)
        self.assertTrue(any(event.event_type == "skill.output_file_rejected" for event in events))

    async def test_llm_failure_after_output_collection_preserves_new_artifact(self) -> None:
        await self._use_skill(
            """import json, os
from pathlib import Path
payload = json.load(__import__('sys').stdin)
text = payload.get('query', '')
out = Path(os.environ['MAF_SKILL_OUTPUT_DIR'])
out.mkdir(parents=True, exist_ok=True)
(out / 'result.txt').write_text(text, encoding='utf-8')
print(json.dumps({'answer': 'ok', 'output_files': [{'path': 'outputs/result.txt'}]}, ensure_ascii=False))
"""
        )
        first = await self.submit_message(conversation_id="conv-llm-fail-artifact", content="请生成文件 第一版", capability_id=self.active_skill_id)
        first_task_id = first.json()["task_id"]
        await self.wait_for_terminal_task(first_task_id)
        first_file = next(
            artifact
            for artifact in (await self.client.get(f"/api/v1/tasks/{first_task_id}/artifacts")).json()["artifacts"]
            if artifact["artifact_type"] == "file"
        )

        async def broken_streamer(prompt: str):
            raise RuntimeError("provider down after file output")
            yield "unreachable"

        await self.reconfigure_runtime(
            skill_roots=(self.workspace / "skills",),
            main_agent_stream_generator=broken_streamer,
            main_agent_llm_config=self._main_agent_llm_config(),
            enable_conversation_memory=False,
        )
        second = await self.submit_message(conversation_id="conv-llm-fail-artifact", content="请生成文件 第二版", capability_id=self.active_skill_id)
        second_task_id = second.json()["task_id"]
        terminal = await self.wait_for_terminal_task(second_task_id)
        self.assertEqual(terminal["status"], "failed")
        second_artifacts = (await self.client.get(f"/api/v1/tasks/{second_task_id}/artifacts")).json()["artifacts"]
        second_file = next(artifact for artifact in second_artifacts if artifact["artifact_type"] == "file")

        old_download = await self.client.get(first_file["download_url"])
        self.assertEqual(old_download.status_code, 404)
        new_download = await self.client.get(second_file["download_url"])
        self.assertEqual(new_download.status_code, 200)
        self.assertIn("第二版", new_download.text)




if __name__ == "__main__":
    import unittest

    unittest.main()

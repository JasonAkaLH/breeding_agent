from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

from src.core.enums import ArtifactType, TaskStatus
from src.core.models import Artifact, Conversation, Task
from src.orchestration.agent_loop.models import (
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
)
from src.orchestration.agent_loop.result_artifacts import (
    AgentSkillResultArtifactStager,
)
from src.orchestration.agent_loop.result_projection import (
    SKILL_RESULT_PROJECTION_REVISION,
    skill_result_artifact_id,
)
from tests.api.support import APITestCase


class SkillOutputArtifactsAPITest(APITestCase):
    async def _result_projection_event(self, task_id: str):
        events = await self.runtime.storage.list_events_for_task(task_id)
        projected = [
            event for event in events if event.event_type == "agent.result_projected"
        ]
        self.assertEqual(len(projected), 1)
        self.assertEqual(
            set(projected[0].payload),
            {
                "artifact_count",
                "capability_id",
                "error_code",
                "original_size_bytes",
                "projected_size_bytes",
                "projection_mode",
                "raw_sha256",
            },
        )
        return projected[0]

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
                        "trim_max_tokens": 450_000,
                        "reasoning_efforts": {
                            "options": [
                                {"value": "minimal", "label": "Minimal"},
                            ],
                            "thinking": {
                                "enabled": {"default": "minimal", "supported": ["minimal"]},
                                "disabled": {"default": "minimal", "supported": ["minimal"]},
                            },
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
        projected = await self._result_projection_event(task_id)
        self.assertEqual(projected.payload["projection_mode"], "inline")
        self.assertIsNone(projected.payload["error_code"])

    async def test_skill_result_is_invisible_before_metadata_and_download_verifies_content(self) -> None:
        now = self.runtime._utcnow_naive()  # noqa: SLF001
        conversation_id = "conv-skill-result"
        task_id = "task-skill-result"
        await self.runtime.storage.save_conversation(
            Conversation(conversation_id, "acc-1", created_at=now, updated_at=now)
        )
        await self.runtime.storage.save_task(
            Task(
                task_id,
                conversation_id,
                "message-skill-result",
                status=TaskStatus.COMPLETED,
                created_at=now,
                updated_at=now,
            )
        )
        run = AgentRun(
            "run-skill-result",
            task_id,
            conversation_id,
            AgentRunStatus.COMPLETED,
            AgentModelBinding("api-test"),
        )
        call = AgentItem(
            "call-skill-result",
            run.run_id,
            task_id,
            1,
            AgentItemKind.TOOL_CALL,
            AgentItemState.COMMITTED,
            "{}\n",
            "0" * 64,
        )
        raw = b'{"articles":[{"title":"complete"}]}\n'
        raw_sha = hashlib.sha256(raw).hexdigest()
        artifact_id = skill_result_artifact_id(
            call_item_id=call.item_id,
            raw_sha256=raw_sha,
            projection_revision=SKILL_RESULT_PROJECTION_REVISION,
        )
        staged = AgentSkillResultArtifactStager(
            file_store=self.runtime.artifact_file_store,
            manifest_root=self.workspace / "manual-skill-result-manifests",
        ).stage(
            run=run,
            call_item=call,
            node_id="node-skill-result",
            canonical_raw_bytes=raw,
            raw_sha256=raw_sha,
            projection_revision=SKILL_RESULT_PROJECTION_REVISION,
            expected_artifact_id=artifact_id,
        )

        hidden = await self.client.get(
            f"/api/v1/artifacts/{artifact_id}/download"
        )
        self.assertEqual(hidden.status_code, 404)
        await self.runtime.storage.save_artifact(
            Artifact(
                artifact_id=artifact_id,
                task_id=task_id,
                producer_node_id="node-skill-result",
                artifact_type=ArtifactType.FILE,
                storage_ref=staged.storage_ref,
                summary=staged.summary,
                is_complete=True,
                created_at=now,
            )
        )
        listed = await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")
        listed.raise_for_status()
        card = next(
            item
            for item in listed.json()["artifacts"]
            if item["artifact_id"] == artifact_id
        )
        self.assertEqual(card["filename"], "skill_result.json")
        self.assertEqual(card["storage_ref"], "")
        download = await self.client.get(card["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, raw)

        metadata = json.loads(staged.storage_ref)
        raw_path = self.runtime.artifact_file_store.open_path(metadata["storage_key"])
        raw_path.write_bytes(b'{"articles":[]}\n')
        raw_path.chmod(0o600)
        drifted = await self.client.get(card["download_url"])
        self.assertEqual(drifted.status_code, 404)
        raw_path.unlink()
        external = self.workspace / "external-result.json"
        external.write_bytes(raw)
        raw_path.symlink_to(external)
        non_regular = await self.client.get(card["download_url"])
        self.assertEqual(non_regular.status_code, 404)

    async def test_large_skill_json_uses_private_transient_receipt_without_artifact(self) -> None:
        await self._use_skill(
            """import json
articles = [
    {
        'title': f'article-{index}',
        'abstract': 'breeding research ' * 700,
        'url': f'https://example.test/articles/{index}',
    }
    for index in range(28)
]
print(json.dumps({
    'articles': articles,
    'structured_content': {'articles': articles},
}, ensure_ascii=False))
"""
        )
        response = await self.submit_message(
            conversation_id="conv-large-skill-result",
            content="生成完整大结果",
            capability_id=self.active_skill_id,
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

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
        self.assertEqual(safe_result["projection_mode"], "transient_staged")
        self.assertEqual(safe_result["projection_revision"], "skill-result-v2")
        self.assertFalse(safe_result["projection_truncated"])
        self.assertNotIn("article-0", json.dumps(safe_result))
        self.assertEqual(result_payload["artifact_refs"], [])
        self.assertRegex(
            safe_result["model_view"]["stage_ref"],
            r"^agent-transient-skill-result:[0-9a-f]{64}$",
        )

        listed = await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")
        listed.raise_for_status()
        self.assertFalse(
            any(
                artifact["filename"] == "skill_result.json"
                for artifact in listed.json()["artifacts"]
            )
        )
        raw_paths = tuple(
            path
            for path in (
                self.workspace / "agent_transient_skill_results" / "raw"
            ).rglob("result.json")
        )
        self.assertEqual(raw_paths, ())
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        skill_node = next(
            node for node in nodes if node.capability_id == self.active_skill_id
        )
        self.assertEqual(str(skill_node.status), "completed")
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertEqual(
            sum(
                event.event_type == "node.completed"
                and event.node_id == skill_node.node_id
                for event in events
            ),
            1,
        )
        projected = await self._result_projection_event(task_id)
        self.assertEqual(projected.payload["projection_mode"], "transient_staged")
        self.assertEqual(projected.payload["artifact_count"], 0)
        self.assertRegex(projected.payload["raw_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(safe_result["raw_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(
            projected.payload["raw_sha256"], safe_result["raw_sha256"]
        )
        self.assertEqual(
            projected.payload["projected_size_bytes"],
            safe_result["projected_size_bytes"],
        )
        self.assertIsNone(projected.payload["error_code"])
        audit_log = (self.workspace / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("article-0", audit_log)
        self.assertNotIn("agent_transient_skill_results", audit_log)

    async def test_invalid_raw_result_commits_typed_failed_node_without_stage(self) -> None:
        await self._use_skill("""print('{\"answer\": NaN}')\n""")
        response = await self.submit_message(
            conversation_id="conv-invalid-skill-result",
            content="生成非法结果",
            capability_id=self.active_skill_id,
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        run = await self.runtime.agent_run_repository.get_run_for_task(task_id)
        assert run is not None
        items = await self.runtime.agent_run_repository.list_items(run.run_id)
        result = next(
            item
            for item in items
            if item.kind is AgentItemKind.TOOL_RESULT
            and item.state is AgentItemState.COMMITTED
        )
        payload = json.loads(result.payload_json)
        self.assertEqual(payload["outcome"], "failed")
        self.assertEqual(payload["safe_error_code"], "agent_result_invalid")
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        skill_node = next(
            node for node in nodes if node.capability_id == self.active_skill_id
        )
        self.assertEqual(str(skill_node.status), "failed")
        artifacts = await self.runtime.storage.list_artifacts_for_task(task_id)
        self.assertFalse(
            any(
                json.loads(artifact.storage_ref).get("source_kind")
                == "skill_result"
                for artifact in artifacts
                if artifact.storage_ref.startswith("{")
            )
        )
        projected = await self._result_projection_event(task_id)
        self.assertEqual(projected.payload["projection_mode"], "invalid")
        self.assertEqual(projected.payload["original_size_bytes"], 0)
        self.assertEqual(projected.payload["projected_size_bytes"], 0)
        self.assertIsNone(projected.payload["raw_sha256"])
        self.assertEqual(projected.payload["artifact_count"], 0)
        self.assertEqual(projected.payload["error_code"], "agent_result_invalid")

    async def test_large_result_stage_failure_commits_typed_failed_node(self) -> None:
        await self._use_skill(
            """import json
print(json.dumps({'rows': ['x' * 10000 for _ in range(20)]}))
"""
        )

        def fail_stage(**_kwargs):
            raise OSError("stage unavailable")

        self.runtime._agent_capability_invoker._stage_transient_result = fail_stage  # noqa: SLF001
        response = await self.submit_message(
            conversation_id="conv-stage-failed-result",
            content="生成大结果",
            capability_id=self.active_skill_id,
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        run = await self.runtime.agent_run_repository.get_run_for_task(task_id)
        assert run is not None
        items = await self.runtime.agent_run_repository.list_items(run.run_id)
        result = next(
            item
            for item in items
            if item.kind is AgentItemKind.TOOL_RESULT
            and item.state is AgentItemState.COMMITTED
        )
        payload = json.loads(result.payload_json)
        self.assertEqual(payload["outcome"], "failed")
        self.assertEqual(
            payload["safe_error_code"],
            "agent_transient_skill_result_stage_failed",
        )
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        skill_node = next(
            node for node in nodes if node.capability_id == self.active_skill_id
        )
        self.assertEqual(str(skill_node.status), "failed")
        projected = await self._result_projection_event(task_id)
        self.assertEqual(
            projected.payload["projection_mode"], "transient_stage_failed"
        )
        self.assertEqual(
            projected.payload["error_code"],
            "agent_transient_skill_result_stage_failed",
        )

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

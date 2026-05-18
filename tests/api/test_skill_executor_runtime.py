from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from unittest.mock import patch

from src.integrations.codex_skills.rust_contract import load_skill_runtime_contract

from tests.api.support import APITestCase


class SkillExecutorRuntimeAPITest(APITestCase):
    async def test_python_subprocess_requires_finalizer_normalizes_answer_without_direct_artifact(self) -> None:
        project_skill_root = self.workspace / 'skill'
        skill_dir = project_skill_root / 'answer-finalizer'
        scripts_dir = skill_dir / 'scripts'
        scripts_dir.mkdir(parents=True)
        (scripts_dir / 'answer.py').write_text(
            'import json\nprint(json.dumps({"answer": "RCBD 设计已完成"}, ensure_ascii=False))',
            encoding='utf-8',
        )
        (skill_dir / 'SKILL.md').write_text(
            """---
name: answer-finalizer
description: 需要主代理汇总的 RCBD 设计
scripts:
  - name: answer
    path: scripts/answer.py
    runtime: python
    auto_run: true
    outputs:
      required:
        - answer
execution:
  mode: python_subprocess
  answer_mode: requires_finalizer
outputs:
  required:
    - answer
---

# Answer Finalizer
执行脚本并交给主代理汇总。
""",
            encoding='utf-8',
        )
        prompts: list[str] = []

        def finalizer(prompt: str, **_kwargs):
            prompts.append(prompt)
            self.assertIn('RCBD 设计已完成', prompt)
            self.assertIn('response_text', prompt)
            return 'finalized'

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            main_agent_stream_generator=finalizer,
        )

        response = await self.submit_message(
            conversation_id='conv-answer-finalizer',
            content='run rcbd',
            capability_id='skill.answer_finalizer',
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()['task_id']
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal['status'], 'completed')

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        skill_node = next(node for node in nodes if node.capability_id == 'skill.answer_finalizer')
        artifacts = await self.runtime.storage.list_artifacts_for_task(task_id)
        self.assertFalse(
            any(artifact.producer_node_id == skill_node.node_id and str(artifact.artifact_type) == 'text' for artifact in artifacts)
        )
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertIn('skill.execution_completed', [event.event_type for event in events])
        self.assertTrue(prompts)

    async def test_python_subprocess_direct_answer_still_generates_text_artifact(self) -> None:
        project_skill_root = self.workspace / 'skill'
        skill_dir = project_skill_root / 'answer-direct'
        scripts_dir = skill_dir / 'scripts'
        scripts_dir.mkdir(parents=True)
        (scripts_dir / 'answer.py').write_text(
            'import json\nprint(json.dumps({"answer": "RCBD 设计已完成"}, ensure_ascii=False))',
            encoding='utf-8',
        )
        (skill_dir / 'SKILL.md').write_text(
            """---
name: answer-direct
description: 直接回答的 RCBD 设计
scripts:
  - name: answer
    path: scripts/answer.py
    runtime: python
    auto_run: true
    outputs:
      required:
        - answer
execution:
  mode: python_subprocess
  answer_mode: direct
outputs:
  required:
    - answer
---

# Answer Direct
执行脚本并直接回答。
""",
            encoding='utf-8',
        )

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
        )

        response = await self.submit_message(
            conversation_id='conv-answer-direct',
            content='run rcbd',
            capability_id='skill.answer_direct',
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()['task_id']
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal['status'], 'completed')

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertEqual([node.capability_id for node in nodes], ['skill.answer_direct'])
        artifacts = await self.runtime.storage.list_artifacts_for_task(task_id)
        self.assertEqual([str(artifact.artifact_type) for artifact in artifacts], ['text'])
        self.assertEqual(artifacts[0].storage_ref, 'RCBD 设计已完成')

    async def test_platform_service_requires_finalizer_normalizes_answer_for_prompt_context(self) -> None:
        project_skill_root = self.workspace / 'skill'
        skill_dir = project_skill_root / 'platform-answer'
        runtime_dir = skill_dir / 'runtime'
        runtime_dir.mkdir(parents=True)
        (runtime_dir / 'platform_handler.py').write_text(
            textwrap.dedent(
                """\
                def build_handler():
                    return lambda context: {"answer": "平台服务结果"}
                """
            ),
            encoding='utf-8',
        )
        (skill_dir / 'SKILL.md').write_text(
            """---
name: platform-answer
description: 平台服务回答
execution:
  mode: platform_service
  answer_mode: requires_finalizer
  trust_scope: project
  handler: skill.platform_answer.handler
  handler_module: runtime/platform_handler.py
---

# Platform Answer
平台服务返回 answer。
""",
            encoding='utf-8',
        )
        prompts: list[str] = []

        def finalizer(prompt: str, **_kwargs):
            prompts.append(prompt)
            self.assertIn('平台服务结果', prompt)
            self.assertIn('response_text', prompt)
            return 'finalized'

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            main_agent_stream_generator=finalizer,
        )

        response = await self.submit_message(
            conversation_id='conv-platform-answer',
            content='run platform',
            capability_id='skill.platform_answer',
        )
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()['task_id'])
        self.assertEqual(terminal['status'], 'completed')
        self.assertTrue(prompts)

    async def test_explicit_python_subprocess_skill_executes_direct_answer(self) -> None:
        project_skill_root = self.workspace / 'skill'
        skill_dir = project_skill_root / 'echo'
        scripts_dir = skill_dir / 'scripts'
        scripts_dir.mkdir(parents=True)
        (scripts_dir / 'echo.py').write_text(
            'import json, sys\npayload = json.load(sys.stdin)\nprint(json.dumps({"response_text": "echo: " + payload["query"]}, ensure_ascii=False))',
            encoding='utf-8',
        )
        (skill_dir / 'SKILL.md').write_text(
            """---
name: echo
description: 直接回显
scripts:
  - name: echo
    path: scripts/echo.py
    runtime: python
execution:
  answer_mode: direct
outputs:
  required:
    - response_text
---

# Echo
执行脚本并直接回答。
""",
            encoding='utf-8',
        )

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
        )

        response = await self.submit_message(
            conversation_id='conv-skill-direct',
            content='hello skill',
            capability_id='skill.echo',
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()['task_id']
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal['status'], 'completed')

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertEqual([node.capability_id for node in nodes], ['skill.echo'])
        artifacts = await self.runtime.storage.list_artifacts_for_task(task_id)
        self.assertEqual([str(artifact.artifact_type) for artifact in artifacts], ['text'])
        self.assertEqual(artifacts[0].storage_ref, 'echo: hello skill')

    async def test_public_python_subprocess_skill_reads_raw_upload_without_prompt_leak(self) -> None:
        project_skill_root = self.workspace / 'skill'
        skill_dir = project_skill_root / 'material-reader'
        scripts_dir = skill_dir / 'scripts'
        scripts_dir.mkdir(parents=True)
        (scripts_dir / 'read_materials.py').write_text(
            textwrap.dedent(
                """\
                import json
                import sys

                payload = json.load(sys.stdin)
                artifacts = payload.get("uploaded_artifacts") or []
                content = artifacts[0].get("content", "") if artifacts else ""
                rows = [line for line in content.splitlines() if line.strip()]
                print(json.dumps(
                    {
                        "answer": f"材料文件已读取：{max(len(rows) - 1, 0)} 行数据",
                        "row_count": max(len(rows) - 1, 0),
                    },
                    ensure_ascii=False,
                ))
                """
            ),
            encoding='utf-8',
        )
        (skill_dir / 'SKILL.md').write_text(
            """---
name: material-reader
description: 读取上传材料文件
scripts:
  - name: read_materials
    path: scripts/read_materials.py
    runtime: python
    auto_run: true
    outputs:
      required:
        - answer
execution:
  mode: python_subprocess
  answer_mode: requires_finalizer
outputs:
  required:
    - answer
---

# Material Reader
读取上传材料。
""",
            encoding='utf-8',
        )
        prompts: list[str] = []
        raw_csv = "plot_id,hyb_check,set\n1,A,A\n2,B,A\n"

        def finalizer(prompt: str, **_kwargs):
            prompts.append(prompt)
            self.assertIn("材料文件已读取：2 行数据", prompt)
            self.assertIn("response_text", prompt)
            self.assertIn("materials.csv", prompt)
            self.assertNotIn(raw_csv, prompt)
            self.assertNotIn("1,A,A", prompt)
            return "finalized"

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            main_agent_stream_generator=finalizer,
        )
        upload = await self.client.post(
            "/api/v1/conversations/conv-material-reader/uploads",
            files={"file": ("materials.csv", raw_csv, "text/csv")},
        )
        self.assertEqual(upload.status_code, 201)
        self.assertNotIn("content", upload.json())

        response = await self.submit_message(
            conversation_id='conv-material-reader',
            content='请根据这份材料文件处理',
            capability_id='skill.material_reader',
            metadata={'upload_ids': [upload.json()['upload_id']]},
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()['task_id']
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal['status'], 'completed')

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertIn('skill.material_reader', [node.capability_id for node in nodes])
        self.assertTrue(prompts)

    async def test_new_conversation_refreshes_executor_mode_skill_and_syncs_instance_support(self) -> None:
        project_skill_root = self.workspace / 'skill'
        project_skill_root.mkdir(parents=True)
        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            main_agent_stream_generator=lambda _prompt, **_kwargs: _single_chunk('finalized'),
        )

        skill_dir = project_skill_root / 'executor-demo'
        scripts_dir = skill_dir / 'scripts'
        scripts_dir.mkdir(parents=True)
        (scripts_dir / 'echo.py').write_text(
            'import json, sys\npayload = json.load(sys.stdin)\nprint(json.dumps({"summary": "processed " + payload["query"]}, ensure_ascii=False))',
            encoding='utf-8',
        )
        (skill_dir / 'SKILL.md').write_text(
            """---
name: executor-demo
description: 需要 finalizer 的技能
scripts:
  - name: echo
    path: scripts/echo.py
    runtime: python
outputs:
  required:
    - summary
---

# Executor Demo
运行脚本。
""",
            encoding='utf-8',
        )

        response = await self.submit_message(
            conversation_id='conv-executor-hot',
            content='refresh skill',
            capability_id='skill.executor_demo',
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()['task_id']
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal['status'], 'completed')

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertEqual({node.capability_id for node in nodes}, {'skill.executor_demo', 'main_agent.respond'})
        instance = next(item for item in self.runtime.instance_registry.list() if item.instance_id == 'inst-skill-local')
        self.assertIn('skill.executor_demo', instance.supported_capabilities)

    async def test_skill_sandbox_enforce_requires_allowlisted_artifact_manifest(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "MAF_SKILL_SANDBOX_ENDPOINT": "http://127.0.0.1:65535",
                    "MAF_RUST_SKILL_RUNTIME_MODE": "enforce",
                    "MAF_SKILL_SANDBOX_ARTIFACT_MANIFEST_PATH": "",
                    "MAF_SKILL_SANDBOX_ARTIFACT_ALLOWLIST_PATH": "",
                },
            ),
            patch("src.api.runtime.SkillSandboxGrpcClient") as client_factory,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "skill_runtime_artifact_untrusted: Rust Skill Sandbox enforce mode requires",
            ):
                await self.reconfigure_runtime(enable_conversation_memory=False)

        client_factory.assert_not_called()

    async def test_skill_sandbox_enforce_validates_artifact_allowlist_before_client_use(self) -> None:
        sentinel_client = object()
        manifest, allowlist, metadata = self._write_skill_sandbox_artifact_trust_files()
        with (
            patch.dict(
                os.environ,
                {
                    "MAF_SKILL_SANDBOX_ENDPOINT": "http://127.0.0.1:65535",
                    "MAF_RUST_SKILL_RUNTIME_MODE": "enforce",
                    "MAF_SKILL_SANDBOX_ARTIFACT_MANIFEST_PATH": str(manifest),
                    "MAF_SKILL_SANDBOX_ARTIFACT_ALLOWLIST_PATH": str(allowlist),
                },
            ),
            patch("src.api.runtime.SkillSandboxGrpcClient", return_value=sentinel_client) as client_factory,
        ):
            await self.reconfigure_runtime(enable_conversation_memory=False)

        client_factory.assert_called_once_with(
            "http://127.0.0.1:65535",
            artifact_provenance=metadata,
            allowed_artifact_checksums=("sha256:skill-sandbox",),
            allowed_cargo_lock_digests=("sha256:cargo-lock",),
        )

    async def test_skill_sandbox_enforce_rejects_manifest_not_exactly_present_in_allowlist(self) -> None:
        manifest, allowlist, _metadata = self._write_skill_sandbox_artifact_trust_files(
            allowlist_overrides={"git_commit": "different-commit"}
        )
        with (
            patch.dict(
                os.environ,
                {
                    "MAF_SKILL_SANDBOX_ENDPOINT": "http://127.0.0.1:65535",
                    "MAF_RUST_SKILL_RUNTIME_MODE": "enforce",
                    "MAF_SKILL_SANDBOX_ARTIFACT_MANIFEST_PATH": str(manifest),
                    "MAF_SKILL_SANDBOX_ARTIFACT_ALLOWLIST_PATH": str(allowlist),
                },
            ),
            patch("src.api.runtime.SkillSandboxGrpcClient") as client_factory,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "skill_runtime_artifact_untrusted: Rust Skill Sandbox artifact manifest is not present",
            ):
                await self.reconfigure_runtime(enable_conversation_memory=False)

        client_factory.assert_not_called()

    def _write_skill_sandbox_artifact_trust_files(
        self,
        *,
        allowlist_overrides: dict[str, object] | None = None,
    ) -> tuple[Path, Path, dict[str, str]]:
        contract = load_skill_runtime_contract()
        manifest_payload = {
            "schema_version": "maf.rust_artifact_provenance.v1",
            "component": "maf_skill_runtime",
            "artifact_id": "maf_skill_sandbox",
            "artifact_kind": "sidecar_binary",
            "artifact_name": "maf-skill-sandbox-linux-x86_64",
            "artifact_sha256": "sha256:skill-sandbox",
            "cargo_lock_sha256": "sha256:cargo-lock",
            "sbom_sha256": "sha256:sbom",
            "provenance_sha256": "sha256:provenance",
            "source": "ci_pipeline",
            "git_commit": "abcdef123456",
            "toolchain": "rustc 1.95.0",
            "target_triple": "x86_64-unknown-linux-gnu",
            "build_profile": "release",
            "cargo_features": ["default"],
            "contract_hashes": {"skill_runtime": contract["schema_hash"]},
            "proto_hashes": {"skill": "maf_skill_proto_v1_20260515"},
        }
        allowlist_entry = dict(manifest_payload)
        if allowlist_overrides:
            allowlist_entry.update(allowlist_overrides)
        allowlist_payload = {
            "schema_version": "maf.rust_artifact_allowlist.v1",
            "allowed_artifacts": [allowlist_entry],
        }
        manifest_path = self.workspace / "skill-sandbox.manifest.json"
        allowlist_path = self.workspace / "skill-sandbox.allowlist.json"
        manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
        allowlist_path.write_text(json.dumps(allowlist_payload), encoding="utf-8")
        metadata = {
            "source": "ci_pipeline",
            "artifact_kind": "skill_sandbox_sidecar_binary",
            "checksum_sha256": "sha256:skill-sandbox",
            "cargo_lock_digest": "sha256:cargo-lock",
            "contract_version": contract["contract_version"],
            "bundle_revision": "abcdef123456",
            "schema_hash": contract["schema_hash"],
            "sbom_digest": "sha256:sbom",
            "provenance_attestation": "sha256:provenance",
        }
        return manifest_path, allowlist_path, metadata


async def _single_chunk(text: str, **_kwargs):
    yield text

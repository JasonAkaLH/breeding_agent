from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from src.integrations.codex_skills.rust_contract import load_skill_runtime_contract

from tests.api.support import APITestCase


class SkillExecutorRuntimeAPITest(APITestCase):
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

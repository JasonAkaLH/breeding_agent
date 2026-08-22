from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from src.api.runtime import build_api_runtime
from src.integrations.agent_skills import (
    ProjectSkillBundleDigestError,
    SkillRuntimeState,
    compute_project_skill_bundle_digest,
)
from src.integrations.agent_skills.bundle_digest import (
    PROJECT_SKILL_BUNDLE_DIGEST_ENV,
)
from src.integrations.agent_skills.catalog import SkillCatalog
from src.integrations.agent_model_gate import validate_agent_model_gate
from tests.api.support import APITestCase, test_llm_config


class ProjectSkillBundleStartupGateTest(unittest.TestCase):
    def _build_runtime(
        self,
        workspace: Path,
        *,
        skill_roots: tuple[Path, ...] | None = None,
        public_skill_roots: tuple[Path, ...] | None = None,
        project_skill_bundle_digest: str | None = None,
    ):
        return build_api_runtime(
            database_path=workspace / "state.sqlite3",
            audit_log_path=workspace / "audit.jsonl",
            master_key_bytes=b"s" * 32,
            main_agent_stream_generator=lambda _prompt, **_kwargs: "test",
            main_agent_llm_config=test_llm_config(),
            enable_platform_llm=False,
            enable_llm_planner=False,
            enable_skill_input_llm=False,
            enable_conversation_title_llm=False,
            enable_conversation_memory=False,
            skill_roots=skill_roots,
            public_skill_roots=public_skill_roots,
            project_skill_bundle_digest=project_skill_bundle_digest,
            enable_user_mcp=False,
            enable_user_mcp_routing=False,
        )

    def test_default_nonempty_root_requires_expected_digest_before_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            root = workspace / "skill"
            APITestCase._write_generic_data_lookup_skill(root)
            previous_cwd = Path.cwd()
            try:
                os.chdir(workspace)
                with patch.dict(
                    os.environ,
                    {
                        "MAF_STATE_STORE_BACKEND": "sqlite",
                        PROJECT_SKILL_BUNDLE_DIGEST_ENV: "",
                    },
                    clear=False,
                ), patch.object(SkillRuntimeState, "__init__") as state_init:
                    with self.assertRaises(ProjectSkillBundleDigestError) as captured:
                        self._build_runtime(workspace)
                failure_log = (workspace / "audit.jsonl").read_text(encoding="utf-8")
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(captured.exception.code, "project_skill_bundle_digest_required")
        state_init.assert_not_called()
        self.assertIn("project_skill_bundle_digest_required", failure_log)
        self.assertNotIn(str(root), failure_log)

    def test_default_root_accepts_expected_digest_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            root = workspace / "skill"
            APITestCase._write_generic_data_lookup_skill(root)
            expected = compute_project_skill_bundle_digest(root).digest
            previous_cwd = Path.cwd()
            try:
                os.chdir(workspace)
                with patch.dict(
                    os.environ,
                    {
                        "MAF_STATE_STORE_BACKEND": "sqlite",
                        PROJECT_SKILL_BUNDLE_DIGEST_ENV: expected,
                    },
                    clear=False,
                ):
                    runtime = self._build_runtime(workspace)
            finally:
                os.chdir(previous_cwd)
            asyncio.run(runtime.shutdown())

    def test_empty_default_root_does_not_require_expected_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            previous_cwd = Path.cwd()
            try:
                os.chdir(workspace)
                with patch.dict(
                    os.environ,
                    {
                        "MAF_STATE_STORE_BACKEND": "sqlite",
                        PROJECT_SKILL_BUNDLE_DIGEST_ENV: "",
                    },
                    clear=False,
                ):
                    runtime = self._build_runtime(workspace)
            finally:
                os.chdir(previous_cwd)
            asyncio.run(runtime.shutdown())

    def test_injected_catalog_does_not_bypass_default_root_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            root = workspace / "skill"
            APITestCase._write_generic_data_lookup_skill(root)
            previous_cwd = Path.cwd()
            try:
                os.chdir(workspace)
                with patch.dict(
                    os.environ,
                    {
                        "MAF_STATE_STORE_BACKEND": "sqlite",
                        PROJECT_SKILL_BUNDLE_DIGEST_ENV: "",
                    },
                    clear=False,
                ):
                    with self.assertRaises(ProjectSkillBundleDigestError) as captured:
                        build_api_runtime(
                            database_path=workspace / "state.sqlite3",
                            audit_log_path=workspace / "audit.jsonl",
                            master_key_bytes=b"s" * 32,
                            main_agent_stream_generator=lambda _prompt, **_kwargs: "test",
                            main_agent_llm_config=test_llm_config(),
                            enable_conversation_memory=False,
                            skill_catalog=SkillCatalog(()),
                            enable_user_mcp=False,
                            enable_user_mcp_routing=False,
                        )
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(captured.exception.code, "project_skill_bundle_digest_required")

    def test_explicit_digest_is_validated_before_catalog_and_records_safe_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            root = workspace / "project-skills"
            APITestCase._write_generic_data_lookup_skill(root)
            expected = compute_project_skill_bundle_digest(root).digest
            order: list[str] = []
            from src.api import runtime as runtime_module

            original_validate = runtime_module.validate_project_skill_bundle_digest
            original_catalog = SkillCatalog.from_roots

            def validate(*args, **kwargs):
                order.append("digest")
                return original_validate(*args, **kwargs)

            def catalog(*args, **kwargs):
                order.append("catalog")
                return original_catalog(*args, **kwargs)

            with patch.dict(
                os.environ, {"MAF_STATE_STORE_BACKEND": "sqlite"}, clear=False
            ), patch.object(
                runtime_module, "validate_project_skill_bundle_digest", side_effect=validate
            ), patch.object(SkillCatalog, "from_roots", side_effect=catalog):
                runtime = self._build_runtime(
                    workspace,
                    skill_roots=(root,),
                    public_skill_roots=(root,),
                    project_skill_bundle_digest=expected,
                )
            asyncio.run(runtime.shutdown())
            records = [
                json.loads(line)
                for line in (workspace / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertLess(order.index("digest"), order.index("catalog"))
        evidence = next(
            record for record in records if record["event_type"] == "skill.project_bundle_validated"
        )
        self.assertEqual(evidence["payload"]["result"], "valid")
        serialized = json.dumps(evidence, ensure_ascii=False)
        self.assertNotIn(str(root), serialized)
        self.assertNotIn(expected, serialized)
        self.assertEqual(evidence["payload"]["digest_prefix"], expected[:19])

    def test_invalid_and_mismatched_digest_fail_before_partial_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            root = workspace / "project-skills"
            APITestCase._write_generic_data_lookup_skill(root)
            for expected, code in (
                ("bad", "project_skill_bundle_digest_invalid"),
                ("sha256:" + "0" * 64, "project_skill_bundle_digest_mismatch"),
            ):
                with self.subTest(code=code), patch.dict(
                    os.environ, {"MAF_STATE_STORE_BACKEND": "sqlite"}, clear=False
                ), patch.object(SkillRuntimeState, "__init__") as state_init:
                    with self.assertRaises(ProjectSkillBundleDigestError) as captured:
                        self._build_runtime(
                            workspace,
                            skill_roots=(root,),
                            public_skill_roots=(root,),
                            project_skill_bundle_digest=expected,
                        )
                    self.assertEqual(captured.exception.code, code)
                    state_init.assert_not_called()

    def test_explicit_test_roots_without_expected_digest_keep_injection_seam(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            root = workspace / "project-skills"
            APITestCase._write_generic_data_lookup_skill(root)
            with patch.dict(
                os.environ,
                {
                    "MAF_STATE_STORE_BACKEND": "sqlite",
                    PROJECT_SKILL_BUNDLE_DIGEST_ENV: "sha256:" + "0" * 64,
                },
                clear=False,
            ):
                runtime = self._build_runtime(
                    workspace,
                    skill_roots=(root,),
                    public_skill_roots=(root,),
                )

        asyncio.run(runtime.shutdown())

    def test_tracked_clean_archive_fixture_is_nonsecret_and_agent_ready(self) -> None:
        fixture = Path("tests/fixtures/unified_agent_loop_clean_archive_config.yaml")
        payload = yaml.safe_load(fixture.read_text(encoding="utf-8"))

        validate_agent_model_gate(payload)
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        for forbidden in ("api_key", "secret", "password", "dsn", "base_url", "endpoint"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.integrations.codex_skills import (
    SkillInputResolutionContext,
    parse_skill_file,
    resolve_skill_inputs,
    resolve_skill_inputs_with_llm,
)


class SkillInputResolutionTest(unittest.IsolatedAsyncioTestCase):
    def _manifest(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        skill_file = Path(tmpdir.name) / "SKILL.md"
        skill_file.write_text(
            """---
name: rcbd-like
triggers:
  - 随机区组
outputs:
  required:
    - answer
scripts:
  - name: run
    path: scripts/run.py
    auto_run: true
    inputs:
      required:
        - query
parameters:
  blocks:
    type: integer
    required: true
    aliases:
      - blocks
      - 区组
      - 区组数
      - 重复
      - 重复数
    patterns:
      - '(?:blocks?|区组数|区组|重复数|重复)\\s*[:：=]?\\s*(\\d+)'
      - '(\\d+)\\s*(?:个|次)?(?:区组|重复|blocks?)'
  material_data:
    type: artifact
    required: true
    source: artifact
---

# RCBD-like
""",
            encoding="utf-8",
        )
        return parse_skill_file(skill_file)

    def test_resolves_required_integer_from_current_query(self) -> None:
        manifest = self._manifest()

        result = resolve_skill_inputs(
            manifest,
            manifest.scripts[0],
            {"query": "请生成随机区组，重复数=2", "uploaded_artifacts": [{"filename": "data.csv"}]},
            SkillInputResolutionContext(
                query="请生成随机区组，重复数=2",
                artifact_summaries=({"filename": "data.csv"},),
            ),
        )

        self.assertEqual(result.payload["blocks"], 2)
        self.assertEqual(result.payload["material_data"], {"available": True, "count": 1})
        self.assertEqual(result.missing, ())
        self.assertEqual(result.sources["blocks"].source, "query")
        self.assertEqual(result.sources["blocks"].confidence, "high")
        self.assertEqual(result.sources["material_data"].source, "artifact")

    def test_resolves_chinese_classifier_from_current_query(self) -> None:
        manifest = self._manifest()

        result = resolve_skill_inputs(
            manifest,
            manifest.scripts[0],
            {"query": "你依据这份文件帮我设计一个随机区组，要求2次重复", "uploaded_artifacts": [{"filename": "data.csv"}]},
            SkillInputResolutionContext(
                query="你依据这份文件帮我设计一个随机区组，要求2次重复",
                artifact_summaries=({"filename": "data.csv"},),
            ),
        )

        self.assertEqual(result.payload["blocks"], 2)
        self.assertEqual(result.sources["blocks"].source, "query")

    def test_resolves_continuation_from_safe_recent_user_message(self) -> None:
        manifest = self._manifest()

        result = resolve_skill_inputs(
            manifest,
            manifest.scripts[0],
            {"query": "按照你的操作继续生成。", "uploaded_artifacts": [{"filename": "data.csv"}]},
            SkillInputResolutionContext(
                query="按照你的操作继续生成。",
                current_user_message="按照你的操作继续生成。",
                recent_user_messages=(
                    "好，按照你的理解进行生成。",
                    "你依据这份文件帮我设计一个随机区组，要求2次重复",
                ),
                artifact_summaries=({"filename": "data.csv"},),
            ),
        )

        self.assertEqual(result.payload["blocks"], 2)
        self.assertEqual(result.missing, ())
        self.assertEqual(result.sources["blocks"].source, "recent_user_message")
        self.assertEqual(result.sources["blocks"].confidence, "medium")

    def test_does_not_resolve_from_assistant_only_claims(self) -> None:
        manifest = self._manifest()

        result = resolve_skill_inputs(
            manifest,
            manifest.scripts[0],
            {"query": "按照你的操作继续生成。"},
            SkillInputResolutionContext(
                query="按照你的操作继续生成。",
                current_user_message="按照你的操作继续生成。",
                recent_user_messages=("好，按照你的理解进行生成。",),
            ),
        )

        self.assertNotIn("blocks", result.payload)
        self.assertEqual(result.missing, ("blocks", "material_data"))

    def test_invalid_explicit_payload_does_not_satisfy_required_parameter(self) -> None:
        manifest = self._manifest()

        result = resolve_skill_inputs(
            manifest,
            manifest.scripts[0],
            {"query": "继续生成", "blocks": "not-a-number"},
            SkillInputResolutionContext(query="继续生成"),
        )

        self.assertNotIn("blocks", result.payload)
        self.assertEqual(result.missing, ("blocks", "material_data"))

    def test_required_artifact_parameter_is_missing_without_artifacts(self) -> None:
        manifest = self._manifest()

        result = resolve_skill_inputs(
            manifest,
            manifest.scripts[0],
            {"query": "请生成随机区组，重复数=2"},
            SkillInputResolutionContext(query="请生成随机区组，重复数=2"),
        )

        self.assertEqual(result.payload["blocks"], 2)
        self.assertNotIn("material_data", result.payload)
        self.assertEqual(result.missing, ("material_data",))

    async def test_llm_fallback_is_not_called_when_deterministic_resolution_is_complete(self) -> None:
        manifest = self._manifest()
        called = False

        async def slot_generator(_prompt: str) -> str:
            nonlocal called
            called = True
            return '{"resolved": {"blocks": {"value": 999}}}'

        result = await resolve_skill_inputs_with_llm(
            manifest,
            manifest.scripts[0],
            {"query": "请生成随机区组，重复数=2", "uploaded_artifacts": [{"filename": "data.csv"}]},
            SkillInputResolutionContext(
                query="请生成随机区组，重复数=2",
                artifact_summaries=({"filename": "data.csv"},),
            ),
            text_generator=slot_generator,
        )

        self.assertFalse(called)
        self.assertEqual(result.payload["blocks"], 2)
        self.assertEqual(result.missing, ())

    async def test_llm_fallback_resolves_missing_scalar_from_safe_recent_user_message(self) -> None:
        manifest = self._manifest()
        prompts: list[str] = []

        async def slot_generator(prompt: str) -> str:
            prompts.append(prompt)
            return '{"resolved": {"blocks": {"value": 2, "source": "recent_user_message", "evidence": "不应进入审计"}}}'

        result = await resolve_skill_inputs_with_llm(
            manifest,
            manifest.scripts[0],
            {"query": "按照你的操作继续生成。", "uploaded_artifacts": [{"filename": "data.csv"}]},
            SkillInputResolutionContext(
                query="按照你的操作继续生成。",
                current_user_message="按照你的操作继续生成。",
                recent_user_messages=("用户刚才说明：重复数这个参数就是 blocks，取两次。",),
                artifact_summaries=({"filename": "data.csv"},),
            ),
            text_generator=slot_generator,
        )

        self.assertEqual(result.payload["blocks"], 2)
        self.assertEqual(result.missing, ())
        self.assertEqual(result.sources["blocks"].source, "llm_slot_resolver:recent_user_message")
        self.assertEqual(result.sources["blocks"].confidence, "medium")
        self.assertIn("blocks", prompts[0])
        self.assertNotIn("不应进入审计", str(result.audit_payload(skill_name="rcbd-like", entrypoint="run")))

    async def test_llm_fallback_profile_omits_entrypoint_and_records_budget_audit(self) -> None:
        manifest = self._manifest()
        calls: list[dict] = []

        async def slot_generator(prompt: str, **kwargs) -> str:
            calls.append({"prompt": prompt, "prompt_profile": kwargs.get("prompt_profile")})
            return '{"resolved": {"blocks": {"value": 2, "source": "recent_user_message"}}}'

        with patch.dict("os.environ", {"MAF_PROMPT_ENVELOPE_MODE": "string"}):
            result = await resolve_skill_inputs_with_llm(
                manifest,
                manifest.scripts[0],
                {
                    "query": "按照你的操作继续生成。",
                    "uploaded_artifacts": [{"filename": "data.csv"}],
                    "metadata": {"trim_max_tokens": 4000},
                },
                SkillInputResolutionContext(
                    query="按照你的操作继续生成。",
                    current_user_message="按照你的操作继续生成。",
                    recent_user_messages=("用户刚才说明：重复数这个参数就是 blocks，取两次。",),
                    artifact_summaries=(
                        {
                            "filename": "data.csv",
                            "summary": "材料表",
                            "content": "RAW_ARTIFACT_CONTENT_SHOULD_NOT_LEAK",
                        },
                    ),
                ),
                text_generator=slot_generator,
            )

        self.assertEqual(result.payload["blocks"], 2)
        self.assertEqual(calls[0]["prompt_profile"]["template_id"], "skill_input_resolver")
        self.assertEqual(calls[0]["prompt_profile"]["final_input_token_budget"], 3000)
        self.assertEqual(result.prompt_profile["template_id"], "skill_input_resolver")
        self.assertNotIn("entrypoint", calls[0]["prompt"])
        self.assertNotIn("scripts/run.py", calls[0]["prompt"])
        self.assertNotIn("RAW_ARTIFACT_CONTENT_SHOULD_NOT_LEAK", calls[0]["prompt"])
        self.assertNotIn("RAW_ARTIFACT_CONTENT_SHOULD_NOT_LEAK", str(result.audit_payload(skill_name="rcbd-like", entrypoint="run")))

    async def test_llm_fallback_rejects_unknown_and_artifact_parameters(self) -> None:
        manifest = self._manifest()

        async def slot_generator(_prompt: str) -> str:
            return (
                '{"resolved": {'
                '"blocks": {"value": 2, "source": "recent_user_message"}, '
                '"material_data": {"value": {"available": true}, "source": "recent_user_message"}, '
                '"unknown": {"value": "x", "source": "recent_user_message"}'
                "}}"
            )

        result = await resolve_skill_inputs_with_llm(
            manifest,
            manifest.scripts[0],
            {"query": "按照你的操作继续生成。"},
            SkillInputResolutionContext(
                query="按照你的操作继续生成。",
                recent_user_messages=("重复数这个参数就是 blocks，取两次。",),
            ),
            text_generator=slot_generator,
        )

        self.assertEqual(result.payload["blocks"], 2)
        self.assertNotIn("material_data", result.payload)
        self.assertNotIn("unknown", result.payload)
        self.assertEqual(result.missing, ("material_data",))

    async def test_llm_fallback_invalid_json_keeps_missing_without_raising(self) -> None:
        manifest = self._manifest()

        async def slot_generator(_prompt: str) -> str:
            return "不是 JSON"

        result = await resolve_skill_inputs_with_llm(
            manifest,
            manifest.scripts[0],
            {"query": "按照你的操作继续生成。", "uploaded_artifacts": [{"filename": "data.csv"}]},
            SkillInputResolutionContext(
                query="按照你的操作继续生成。",
                recent_user_messages=("重复数这个参数就是 blocks，取两次。",),
                artifact_summaries=({"filename": "data.csv"},),
            ),
            text_generator=slot_generator,
        )

        self.assertNotIn("blocks", result.payload)
        self.assertEqual(result.missing, ("blocks",))
        self.assertIn("llm_invalid_json", result.diagnostics)

    async def test_llm_fallback_exception_keeps_missing_without_raising(self) -> None:
        manifest = self._manifest()

        async def slot_generator(_prompt: str) -> str:
            raise RuntimeError("provider unavailable with sensitive detail")

        result = await resolve_skill_inputs_with_llm(
            manifest,
            manifest.scripts[0],
            {"query": "按照你的操作继续生成。", "uploaded_artifacts": [{"filename": "data.csv"}]},
            SkillInputResolutionContext(
                query="按照你的操作继续生成。",
                recent_user_messages=("重复数这个参数就是 blocks，取两次。",),
                artifact_summaries=({"filename": "data.csv"},),
            ),
            text_generator=slot_generator,
        )

        self.assertNotIn("blocks", result.payload)
        self.assertEqual(result.missing, ("blocks",))
        self.assertEqual(result.diagnostics, ("llm_failed",))

    async def test_llm_fallback_only_accepts_still_missing_fields_when_other_scalar_is_resolved(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        skill_file = Path(tmpdir.name) / "SKILL.md"
        skill_file.write_text(
            """---
name: scalar-mix
triggers:
  - 生成
scripts:
  - name: run
    path: scripts/run.py
    auto_run: true
parameters:
  blocks:
    type: integer
    required: true
    aliases:
      - 重复
    patterns:
      - '(\\d+)\\s*(?:个|次)?重复'
  plots:
    type: integer
    required: true
    aliases:
      - 小区
---

# Scalar mix
""",
            encoding="utf-8",
        )
        manifest = parse_skill_file(skill_file)

        async def slot_generator(_prompt: str) -> str:
            return (
                '{"resolved": {'
                '"blocks": {"value": 999, "source": "recent_user_message"}, '
                '"plots": {"value": 12, "source": "recent_user_message"}'
                "}}"
            )

        result = await resolve_skill_inputs_with_llm(
            manifest,
            manifest.scripts[0],
            {"query": "请生成随机区组，2次重复"},
            SkillInputResolutionContext(
                query="请生成随机区组，2次重复",
                recent_user_messages=("小区数是 12。",),
            ),
            text_generator=slot_generator,
        )

        self.assertEqual(result.payload["blocks"], 2)
        self.assertEqual(result.payload["plots"], 12)
        self.assertEqual(result.sources["blocks"].source, "query")
        self.assertEqual(result.sources["plots"].source, "llm_slot_resolver:recent_user_message")
        self.assertEqual(result.missing, ())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from src.integrations.llm_request_options import resolve_llm_request_options
from src.integrations.model_editions import model_reasoning_effort_configs


def _config() -> dict:
    return {
        "model_editions": {
            "default": "deepseek-v4-flash-260425",
            "options": [
                {
                    "value": "deepseek-v4-flash-260425",
                    "label": "DeepSeek V4 Flash",
                    "reasoning_efforts": {
                        "default": "minimal",
                        "disabled_default": "minimal",
                        "options": [
                            {"value": "minimal", "label": "最低", "allow_when_thinking_disabled": True},
                            {"value": "high", "label": "高", "allow_when_thinking_disabled": False},
                            {"value": "max", "label": "最高", "allow_when_thinking_disabled": False},
                        ],
                    },
                },
                {
                    "value": "doubao-seed-2-1-pro-260628",
                    "label": "豆包Seed 2.1 Pro",
                    "reasoning_efforts": {
                        "default": "minimal",
                        "disabled_default": "minimal",
                        "options": [
                            {"value": "minimal", "label": "最低", "allow_when_thinking_disabled": True},
                            {"value": "low", "label": "低", "allow_when_thinking_disabled": False},
                            {"value": "medium", "label": "中", "allow_when_thinking_disabled": False},
                            {"value": "high", "label": "高", "allow_when_thinking_disabled": False},
                        ],
                    },
                },
            ],
        }
    }


class LLMRequestOptionsTest(unittest.TestCase):
    def setUp(self) -> None:
        config = _config()
        self.registry = model_reasoning_effort_configs(config)
        self.default_model = "deepseek-v4-flash-260425"

    def resolve(self, metadata: dict):
        return resolve_llm_request_options(
            metadata,
            model_reasoning_configs=self.registry,
            default_model_edition=self.default_model,
        )

    def test_enabled_thinking_uses_model_specific_effort(self) -> None:
        options = self.resolve({
            "model_edition": "doubao-seed-2-1-pro-260628",
            "deep_thinking": True,
            "main_agent_reasoning_effort": "medium",
        })

        self.assertTrue(options.thinking)
        self.assertEqual(options.model_edition, "doubao-seed-2-1-pro-260628")
        self.assertEqual(options.reasoning_effort, "medium")
        self.assertEqual(options.requested_reasoning_effort, "medium")

    def test_rejects_unknown_model_specific_effort(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported reasoning_effort"):
            self.resolve({
                "model_edition": "doubao-seed-2-1-pro-260628",
                "deep_thinking": True,
                "main_agent_reasoning_effort": "max",
            })

    def test_disabled_thinking_uses_disabled_default_when_effort_omitted(self) -> None:
        options = self.resolve({"model_edition": "doubao-seed-2-1-pro-260628", "deep_thinking": False})

        self.assertFalse(options.thinking)
        self.assertEqual(options.reasoning_effort, "minimal")

    def test_rejects_disabled_disallowed_effort_before_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not allow reasoning_effort=high"):
            self.resolve({
                "model_edition": "doubao-seed-2-1-pro-260628",
                "deep_thinking": False,
                "main_agent_reasoning_effort": "high",
            })

    def test_default_model_is_used_when_request_omits_model_edition(self) -> None:
        options = self.resolve({"deep_thinking": True, "main_agent_reasoning_effort": "max"})

        self.assertEqual(options.model_edition, "deepseek-v4-flash-260425")
        self.assertEqual(options.reasoning_effort, "max")


if __name__ == "__main__":
    unittest.main()

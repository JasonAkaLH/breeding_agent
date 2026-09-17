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
                        "options": [
                            {"value": "minimal", "label": "最低"},
                            {"value": "high", "label": "高"},
                            {"value": "max", "label": "最高"},
                        ],
                        "thinking": {
                            "enabled": {"default": "high", "supported": ["minimal", "high", "max"]},
                            "disabled": {"default": "minimal", "supported": ["minimal", "high", "max"]},
                        },
                    },
                },
                {
                    "value": "doubao-seed-2-1-pro-260628",
                    "label": "豆包Seed 2.1 Pro",
                    "reasoning_efforts": {
                        "options": [
                            {"value": "minimal", "label": "最低"},
                            {"value": "low", "label": "低"},
                            {"value": "medium", "label": "中"},
                            {"value": "high", "label": "高"},
                        ],
                        "thinking": {
                            "enabled": {"default": "high", "supported": ["minimal", "low", "medium", "high"]},
                            "disabled": {"default": "minimal", "supported": ["minimal"]},
                        },
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

    def resolve(self, metadata: dict, *, fallback: str | None = None):
        return resolve_llm_request_options(
            metadata,
            fallback_reasoning_effort=fallback,
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
        with self.assertRaisesRegex(ValueError, "Unknown reasoning_effort"):
            self.resolve({
                "model_edition": "doubao-seed-2-1-pro-260628",
                "deep_thinking": True,
                "main_agent_reasoning_effort": "max",
            })

    def test_omitted_effort_uses_each_state_default(self) -> None:
        enabled = self.resolve({"model_edition": "doubao-seed-2-1-pro-260628", "deep_thinking": True})
        options = self.resolve({"model_edition": "doubao-seed-2-1-pro-260628", "deep_thinking": False})

        self.assertTrue(enabled.thinking)
        self.assertEqual(enabled.reasoning_effort, "high")
        self.assertFalse(options.thinking)
        self.assertEqual(options.reasoning_effort, "minimal")

    def test_configured_state_default_wins_over_factory_fallback(self) -> None:
        options = self.resolve(
            {"model_edition": "deepseek-v4-flash-260425", "deep_thinking": True},
            fallback="minimal",
        )

        self.assertEqual(options.reasoning_effort, "high")

    def test_deepseek_allows_high_when_thinking_is_disabled(self) -> None:
        options = self.resolve({
            "model_edition": "deepseek-v4-flash-260425",
            "deep_thinking": False,
            "main_agent_reasoning_effort": "high",
        })

        self.assertEqual(options.reasoning_effort, "high")

    def test_rejects_disabled_disallowed_effort_before_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not support reasoning_effort=high when thinking=disabled"):
            self.resolve({
                "model_edition": "doubao-seed-2-1-pro-260628",
                "deep_thinking": False,
                "main_agent_reasoning_effort": "high",
            })

    def test_default_model_is_used_when_request_omits_model_edition(self) -> None:
        options = self.resolve({"deep_thinking": True, "main_agent_reasoning_effort": "max"})

        self.assertEqual(options.model_edition, "deepseek-v4-flash-260425")
        self.assertEqual(options.reasoning_effort, "max")

    def test_rejects_disabling_model_without_disabled_efforts(self) -> None:
        config = _config()
        config["model_editions"]["options"][0]["reasoning_efforts"]["thinking"]["disabled"] = {
            "default": None,
            "supported": [],
        }
        registry = model_reasoning_effort_configs(config)

        with self.assertRaisesRegex(ValueError, "does not support thinking=disabled"):
            resolve_llm_request_options(
                {"model_edition": "deepseek-v4-flash-260425", "deep_thinking": False},
                model_reasoning_configs=registry,
                default_model_edition=self.default_model,
            )


if __name__ == "__main__":
    unittest.main()

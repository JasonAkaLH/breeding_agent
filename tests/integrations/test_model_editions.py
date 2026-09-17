from __future__ import annotations

import copy
import unittest

from src.integrations.model_editions import (
    model_reasoning_effort_config,
    validate_model_reasoning_effort_configs,
)


def _config() -> dict:
    return {
        "model_editions": {
            "default": "model-a",
            "options": [
                {
                    "value": "model-a",
                    "reasoning_efforts": {
                        "options": [
                            {"value": "minimal", "label": "最低"},
                            {"value": "high", "label": "高"},
                            {"value": "max"},
                        ],
                        "thinking": {
                            "enabled": {
                                "default": "high",
                                "supported": ["minimal", "high", "max"],
                            },
                            "disabled": {
                                "default": "minimal",
                                "supported": ["minimal", "high"],
                            },
                        },
                    },
                }
            ],
        }
    }


class ModelReasoningEffortConfigTest(unittest.TestCase):
    def test_parses_state_policies_and_preserves_catalog_order(self) -> None:
        cfg = model_reasoning_effort_config("model-a", config=_config())

        self.assertIsNotNone(cfg)
        assert cfg is not None
        self.assertEqual(cfg.option_values(), ("minimal", "high", "max"))
        self.assertEqual([option.label for option in cfg.options], ["最低", "高", "max"])
        self.assertEqual(cfg.supported_values(True), ("minimal", "high", "max"))
        self.assertEqual(cfg.supported_values(False), ("minimal", "high"))
        self.assertEqual(cfg.default_for(True), "high")
        self.assertEqual(cfg.default_for(False), "minimal")
        self.assertTrue(cfg.supports("high", thinking_enabled=False))
        self.assertFalse(cfg.supports("max", thinking_enabled=False))

    def test_disabled_empty_supported_with_null_default_is_valid(self) -> None:
        config = _config()
        config["model_editions"]["options"][0]["reasoning_efforts"]["thinking"]["disabled"] = {
            "default": None,
            "supported": [],
        }

        validate_model_reasoning_effort_configs(config)

    def test_rejects_empty_options(self) -> None:
        config = _config()
        config["model_editions"]["options"][0]["reasoning_efforts"]["options"] = []
        with self.assertRaisesRegex(ValueError, "options must not be empty"):
            validate_model_reasoning_effort_configs(config)

    def test_rejects_duplicate_option_values(self) -> None:
        config = _config()
        options = config["model_editions"]["options"][0]["reasoning_efforts"]["options"]
        options.append({"value": "high", "label": "duplicate"})
        with self.assertRaisesRegex(ValueError, "duplicate reasoning_efforts option value"):
            validate_model_reasoning_effort_configs(config)

    def test_rejects_unknown_supported_value(self) -> None:
        config = _config()
        enabled = config["model_editions"]["options"][0]["reasoning_efforts"]["thinking"]["enabled"]
        enabled["supported"].append("unknown")
        with self.assertRaisesRegex(ValueError, "enabled.supported must reference options"):
            validate_model_reasoning_effort_configs(config)

    def test_rejects_duplicate_supported_value(self) -> None:
        config = _config()
        enabled = config["model_editions"]["options"][0]["reasoning_efforts"]["thinking"]["enabled"]
        enabled["supported"].append("high")
        with self.assertRaisesRegex(ValueError, "duplicate enabled.supported value"):
            validate_model_reasoning_effort_configs(config)

    def test_rejects_orphan_catalog_option(self) -> None:
        config = _config()
        thinking = config["model_editions"]["options"][0]["reasoning_efforts"]["thinking"]
        thinking["enabled"]["supported"].remove("max")
        with self.assertRaisesRegex(ValueError, "orphan reasoning_efforts option"):
            validate_model_reasoning_effort_configs(config)

    def test_rejects_empty_enabled_supported(self) -> None:
        config = _config()
        enabled = config["model_editions"]["options"][0]["reasoning_efforts"]["thinking"]["enabled"]
        enabled["default"] = None
        enabled["supported"] = []
        with self.assertRaisesRegex(ValueError, "enabled.supported must not be empty"):
            validate_model_reasoning_effort_configs(config)

    def test_rejects_state_default_outside_supported(self) -> None:
        for state in ("enabled", "disabled"):
            with self.subTest(state=state):
                config = copy.deepcopy(_config())
                policy = config["model_editions"]["options"][0]["reasoning_efforts"]["thinking"][state]
                policy["default"] = "max"
                if state == "enabled":
                    policy["supported"].remove("max")
                with self.assertRaisesRegex(ValueError, rf"{state}.default must reference {state}.supported"):
                    validate_model_reasoning_effort_configs(config)

    def test_rejects_disabled_default_when_supported_is_empty(self) -> None:
        config = _config()
        config["model_editions"]["options"][0]["reasoning_efforts"]["thinking"]["disabled"] = {
            "default": "minimal",
            "supported": [],
        }
        with self.assertRaisesRegex(ValueError, "disabled.default must be null"):
            validate_model_reasoning_effort_configs(config)

    def test_rejects_legacy_schema(self) -> None:
        for legacy in (
            {
                "default": "minimal",
                "disabled_default": "minimal",
                "options": [{"value": "minimal", "allow_when_thinking_disabled": True}],
            },
            {
                "options": [{"value": "minimal", "allow_when_thinking_disabled": True}],
                "thinking": {
                    "enabled": {"default": "minimal", "supported": ["minimal"]},
                    "disabled": {"default": "minimal", "supported": ["minimal"]},
                },
            },
        ):
            with self.subTest(legacy=legacy):
                config = _config()
                config["model_editions"]["options"][0]["reasoning_efforts"] = legacy
                with self.assertRaisesRegex(ValueError, "legacy reasoning_efforts fields"):
                    validate_model_reasoning_effort_configs(config)


if __name__ == "__main__":
    unittest.main()

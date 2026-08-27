from __future__ import annotations

import unittest

from src.integrations.agent_model_gate import (
    agent_ready_model_edition_options,
    evaluate_agent_model_gate,
    validate_agent_model_edition,
    validate_agent_model_gate,
)


def _reasoning():
    return {
        "default": "minimal",
        "disabled_default": "minimal",
        "options": [{"value": "minimal", "label": "minimal", "allow_when_thinking_disabled": True}],
    }


def _capabilities(**overrides):
    value = {
        "supports_messages": True,
        "roles": ["system", "user", "assistant", "tool"],
        "supports_native_tools": True,
        "supports_required_tool_choice": True,
        "supports_streamed_tool_calls": True,
    }
    value.update(overrides)
    return value


def _config(default="ready"):
    return {
        "agent_protocol_max_retries": 1,
        "model_editions": {
            "default": default,
            "options": [
                {"value": "ready", "reasoning_efforts": _reasoning(), "agent_capabilities": _capabilities()},
                {"value": "missing-profile", "reasoning_efforts": _reasoning()},
                {"value": "missing-role", "reasoning_efforts": _reasoning(), "agent_capabilities": _capabilities(roles=["system", "user", "assistant"])},
                {"value": "non-stream", "reasoning_efforts": _reasoning(), "agent_capabilities": _capabilities(supports_streamed_tool_calls=False, supports_non_stream_agent_sample=True)},
            ],
        },
    }


class AgentModelGateTest(unittest.TestCase):
    def test_filters_non_default_unready_editions_and_accepts_explicit_non_stream_fallback(self) -> None:
        report = validate_agent_model_gate(_config())
        self.assertEqual(report.ready_editions, ("ready", "non-stream"))
        self.assertEqual([option.value for option in agent_ready_model_edition_options(_config())], ["ready", "non-stream"])
        self.assertIn("agent_capabilities", report.rejected_editions["missing-profile"])
        self.assertIn("roles=tool", report.rejected_editions["missing-role"])

    def test_default_unready_edition_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Default model edition is not Agent-ready"):
            validate_agent_model_gate(_config(default="missing-profile"))

    def test_extra_message_role_is_not_agent_ready(self) -> None:
        config = _config()
        config["model_editions"]["options"][0]["agent_capabilities"]["roles"].append("developer")

        report = evaluate_agent_model_gate(config)

        self.assertEqual(report.rejected_editions["ready"], ("roles=unsupported:developer",))

    def test_public_selection_rejects_unready_edition(self) -> None:
        self.assertEqual(validate_agent_model_edition("ready", config=_config()), "ready")
        with self.assertRaisesRegex(ValueError, "non-Agent-ready"):
            validate_agent_model_edition("missing-role", config=_config())

    def test_gate_rejects_invalid_protocol_retry_config_at_startup(self) -> None:
        config = _config()
        config["agent_protocol_max_retries"] = "request-value"
        with self.assertRaisesRegex(ValueError, "agent_protocol_max_retries"):
            validate_agent_model_gate(config)

    def test_evaluation_report_contains_no_provider_credentials(self) -> None:
        config = _config()
        config.update({"api_key": "SECRET", "base_url": "https://secret.example"})
        report = evaluate_agent_model_gate(config)
        self.assertNotIn("SECRET", str(report))
        self.assertNotIn("secret.example", str(report))

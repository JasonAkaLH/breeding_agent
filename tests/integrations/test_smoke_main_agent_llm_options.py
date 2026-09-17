from __future__ import annotations

import argparse
import unittest

from scripts.smoke_main_agent_llm import _build_submit_request


def _args(**overrides) -> argparse.Namespace:
    values = {
        "conversation_id": "conversation-1",
        "message": "hello",
        "model_edition": None,
        "thinking": "disabled",
        "reasoning_effort": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class SmokeMainAgentLLMOptionsTest(unittest.TestCase):
    def test_omits_unspecified_model_and_effort(self) -> None:
        request = _build_submit_request(_args())

        self.assertIsNone(request.model_edition)
        self.assertEqual(request.metadata, {"deep_thinking": False})

    def test_maps_explicit_model_thinking_and_effort(self) -> None:
        request = _build_submit_request(
            _args(
                model_edition="model-a",
                thinking="enabled",
                reasoning_effort="xhigh",
            )
        )

        self.assertEqual(request.model_edition, "model-a")
        self.assertEqual(
            request.metadata,
            {
                "deep_thinking": True,
                "main_agent_reasoning_effort": "xhigh",
            },
        )


if __name__ == "__main__":
    unittest.main()

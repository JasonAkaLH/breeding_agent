from __future__ import annotations

import unittest
import json
from pathlib import Path

from src.orchestration.agent_loop.models import (
    AgentCallOutcomeStatus,
    AgentItemKind,
    AgentRunStatus,
)
from src.storage.agent_payload import canonicalize_agent_payload


class AgentStorageConformanceContractTest(unittest.TestCase):
    def test_shared_python_rust_canonical_vectors_match(self) -> None:
        vectors = json.loads(
            (Path(__file__).resolve().parents[1] / "fixtures" / "agent_payload_vectors.json").read_text(encoding="utf-8")
        )
        for vector in vectors:
            with self.subTest(vector=vector["name"]):
                payload = canonicalize_agent_payload(vector["value"])
                self.assertEqual(payload.json_text, vector["canonical_json"])
                self.assertEqual(payload.size_bytes, vector["size_bytes"])
                self.assertEqual(payload.sha256, vector["sha256"])

    def test_closed_status_and_item_kind_values_are_frozen(self) -> None:
        self.assertEqual(
            {item.value for item in AgentRunStatus},
            {"running", "waiting_for_input", "waiting_for_dependency", "completed", "failed", "cancelled"},
        )
        self.assertEqual(
            {item.value for item in AgentItemKind},
            {"user_message", "assistant_message", "tool_call", "tool_result", "skill_activation", "context_summary", "continuation"},
        )
        self.assertEqual(
            {item.value for item in AgentCallOutcomeStatus},
            {
                "aborted",
                "completed",
                "failed",
                "waiting_for_dependency",
                "waiting_for_input",
            },
        )

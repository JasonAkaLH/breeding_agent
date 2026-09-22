from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from src.capabilities.main_agent.prompt_builder import MAIN_AGENT_FACTUAL_GROUNDING_LINES
from src.integrations import agent_prompt_diagnostics as diagnostics
from src.integrations.openai_agent_model_adapter import OpenAIAgentModelAdapter
from src.orchestration.agent_loop.models import AgentMessage, AgentProtocolRetryPolicy
from tests.integrations.test_agent_model_adapter import _Completions, _Stream, _chunk, _request


class AgentPromptDiagnosticsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.control = Path(self.directory.name) / "control.json"
        self.enterContext(patch.object(diagnostics, "_CONTROL_PATH", self.control))
        self.enterContext(patch.dict(os.environ, {"MAF_API_ENV": "dev"}))
        self.log = self.enterContext(patch.object(diagnostics._LOGGER, "warning"))

    def _arm(self, *, expires_at: float | None = None) -> None:
        self.control.write_text(json.dumps({
            "capture_id": "a" * 32,
            "expires_at": expires_at if expires_at is not None else time.time() + 600,
        }))

    def _request(self, *, task: str = "task-first", revision: int = 1):
        return replace(
            _request(),
            request_id=f"agent-sample:agent-run:{task}:r{revision}",
            messages=(
                AgentMessage("system", "\n".join(MAIN_AGENT_FACTUAL_GROUNDING_LINES)),
                AgentMessage("user", "PRIVATE_USER_TEXT"),
                AgentMessage("tool", "PRIVATE_TOOL_DATA", tool_call_id="previous-call"),
            ),
        )

    def _payload(self):
        return {
            "model": "edition-a", "stream": True,
            "messages": [{"role": m.role, "content": m.content} for m in self._request().messages],
        }

    def _records(self):
        return [json.loads(call.args[1]) for call in self.log.call_args_list]

    def test_disabled_expired_invalid_and_non_dev_do_not_capture(self) -> None:
        request_id = self._request().request_id
        payload = self._payload()
        self.assertIsNone(diagnostics.capture_prompt_request(request_id, payload, attempt=1))
        self._arm(expires_at=time.time() - 1)
        self.assertIsNone(diagnostics.capture_prompt_request(request_id, payload, attempt=1))
        for expires_at in (float("nan"), float("inf"), time.time() + 3600):
            self._arm(expires_at=expires_at)
            self.assertIsNone(diagnostics.capture_prompt_request(request_id, payload, attempt=1))
        self.control.write_text("invalid json")
        self.assertIsNone(diagnostics.capture_prompt_request(request_id, payload, attempt=1))
        self._arm()
        with patch.dict(os.environ, {"MAF_API_ENV": "prod"}):
            self.assertIsNone(diagnostics.capture_prompt_request(request_id, payload, attempt=1))
        self.assertIsNone(diagnostics.capture_prompt_request("startup-gate", payload, attempt=1))
        self.log.assert_not_called()
        self.assertEqual(list(self.control.parent.glob("*.task")), [])

    def test_claim_survives_later_rounds_and_excludes_other_tasks(self) -> None:
        self._arm()
        payload = self._payload()
        first = diagnostics.capture_prompt_request(self._request().request_id, payload, attempt=1)
        other = diagnostics.capture_prompt_request(self._request(task="task-other").request_id, payload, attempt=1)
        resumed = diagnostics.capture_prompt_request(self._request(revision=8).request_id, payload, attempt=1)
        self.assertIsNotNone(first)
        self.assertIsNone(other)
        self.assertIsNotNone(resumed)
        self.assertEqual([r["task_id"] for r in self._records()], ["task-first", "task-first"])
        self.assertEqual(self._records()[1]["grounding_rules_in_system"], [True] * 6)
        claim = next(self.control.parent.glob("*.task"))
        self.assertEqual(claim.stat().st_mode & 0o777, 0o600)
        self.control.unlink()
        self.assertIsNone(diagnostics.capture_prompt_request(self._request(revision=9).request_id, payload, attempt=1))

    def test_matches_full_rules_only_in_system_and_records_sdk_body_override(self) -> None:
        self._arm()
        payload = self._payload()
        payload["messages"] = [
            {"role": "system", "content": "[事实与解释边界]"},
            {"role": "user", "content": "\n".join(MAIN_AGENT_FACTUAL_GROUNDING_LINES)},
        ]
        diagnostics.capture_prompt_request(self._request().request_id, payload, attempt=1)
        self.assertEqual(self._records()[-1]["grounding_rules_in_system"], [False] * 6)
        payload["extra_body"] = {"messages": [
            {"role": "system", "content": "\n".join(MAIN_AGENT_FACTUAL_GROUNDING_LINES[:-1])}
        ]}
        diagnostics.capture_prompt_request(self._request().request_id, payload, attempt=2)
        record = self._records()[-1]
        self.assertEqual(record["grounding_rules_in_system"], [True] * 5 + [False])
        self.assertTrue(record["extra_body_overrides_messages"])
        self.assertEqual(record["rule_system_message_indices"], [[0]] * 5 + [[]])

    async def test_stream_request_and_response_correlate_without_body_leakage(self) -> None:
        self._arm()
        completions = _Completions([_Stream([_chunk(text="PRIVATE_REPLY", finish="stop", response_id="provider-123")])])
        adapter = OpenAIAgentModelAdapter(completions=completions, model="edition-a")
        request = self._request(revision=8)
        expected = adapter._request_payload(request, stream=True)
        sample = await adapter.sample_agent(request)
        self.assertEqual(completions.calls, [expected])
        self.assertEqual(sample.visible_text, "PRIVATE_REPLY")
        records = self._records()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["grounding_rules_in_system"], [True] * 6)
        self.assertEqual(records[0]["message_roles"], ["system", "user", "tool"])
        self.assertEqual(records[0]["request_id"], records[1]["request_id"])
        self.assertEqual(records[1]["response_id"], "provider-123")
        self.assertEqual(records[1]["finish_reason"], "stop")
        self.assertEqual(records[1]["tool_call_count"], 0)
        for secret in ["PRIVATE_USER_TEXT", "PRIVATE_TOOL_DATA", "PRIVATE_REPLY", *MAIN_AGENT_FACTUAL_GROUNDING_LINES]:
            self.assertNotIn(secret, json.dumps(records, ensure_ascii=False))

    async def test_non_stream_diagnostic_matches_actual_sdk_serialized_messages(self) -> None:
        self._arm()
        bodies = []

        def respond(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "provider-sdk-123", "object": "chat.completion", "created": 0,
                "model": "edition-a", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "PRIVATE_REPLY"}}],
            })

        async with AsyncOpenAI(
            api_key="PRIVATE_API_KEY", base_url="https://provider.invalid/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ) as client:
            messages = [{"role": "system", "content": "\n".join(MAIN_AGENT_FACTUAL_GROUNDING_LINES[:2])}]
            adapter = OpenAIAgentModelAdapter(
                completions=client.chat.completions, model="edition-a", stream=False,
                request_options={"extra_body": {"messages": messages}},
            )
            await adapter.sample_agent(self._request())
        self.assertEqual(bodies[0]["messages"], messages)
        records = self._records()
        self.assertEqual(records[0]["grounding_rules_in_system"], [True, True] + [False] * 4)
        self.assertEqual(records[1]["response_id"], "provider-sdk-123")
        self.assertNotIn("PRIVATE_API_KEY", json.dumps(records))

    async def test_protocol_retry_attempts_are_separate(self) -> None:
        self._arm()
        completions = _Completions([
            _Stream([_chunk(text="", finish="stop", response_id="provider-empty")]),
            _Stream([_chunk(text="ok", finish="stop", response_id="provider-good")]),
        ])
        adapter = OpenAIAgentModelAdapter(
            completions=completions, model="edition-a",
            retry_policy=AgentProtocolRetryPolicy(max_retries=1),
        )
        await adapter.sample_agent(self._request())
        self.assertEqual([r["attempt"] for r in self._records()], [1, 1, 2, 2])

    async def test_diagnostic_io_and_logging_failures_do_not_break_sampling(self) -> None:
        self._arm()
        for failure in ("claim", "log"):
            with self.subTest(failure=failure):
                context = (
                    patch.object(diagnostics.os, "open", side_effect=OSError("PRIVATE_ERROR"))
                    if failure == "claim"
                    else patch.object(diagnostics._LOGGER, "warning", side_effect=RuntimeError("PRIVATE_ERROR"))
                )
                with context:
                    adapter = OpenAIAgentModelAdapter(
                        completions=_Completions([_Stream([_chunk(text="ok", finish="stop")])]),
                        model="edition-a",
                    )
                    sample = await adapter.sample_agent(self._request())
                    self.assertEqual(sample.visible_text, "ok")


if __name__ == "__main__":
    unittest.main()

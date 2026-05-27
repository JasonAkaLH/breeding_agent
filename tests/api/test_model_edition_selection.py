from __future__ import annotations

import json
from typing import Any

from tests.api.support import APITestCase


class ModelEditionSelectionAPITest(APITestCase):
    def _model_config(self) -> dict[str, Any]:
        return {
            "api_key": "test",
            "base_url": "http://example.test",
            "model_editions": {
                "default": "deepseek-v4-flash-260425",
                "options": [
                    {"value": "deepseek-v4-flash-260425", "label": "DeepSeek V4 Flash", "trim_max_tokens": 1024000},
                    {"value": "deepseek-v4-pro-260425", "label": "DeepSeek V4 Pro", "trim_max_tokens": 1024000},
                ],
            },
        }

    async def test_model_editions_endpoint_returns_runtime_configured_options(self) -> None:
        await self.reconfigure_runtime(main_agent_llm_config=self._model_config())

        response = await self.client.get("/api/v1/config/model-editions")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "default_model_edition": "deepseek-v4-flash-260425",
                "options": [
                    {"value": "deepseek-v4-flash-260425", "label": "DeepSeek V4 Flash"},
                    {"value": "deepseek-v4-pro-260425", "label": "DeepSeek V4 Pro"},
                ],
            },
        )

    async def test_submit_message_rejects_unknown_model_edition(self) -> None:
        await self.reconfigure_runtime(main_agent_llm_config=self._model_config())

        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-model-invalid",
                "content": "你好",
                "routing_mode": "auto",
                "capability_id": None,
                "model_edition": "unknown-model",
                "metadata": {},
            },
        )

        self.assertEqual(response.status_code, 400, response.text)

    async def test_selected_model_edition_reaches_planner_and_main_agent_runtime(self) -> None:
        class RecordingLLM:
            instances: list["RecordingLLM"] = []

            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs
                self.model_edition = kwargs["config"]["model_edition"]
                self.calls: list[dict[str, Any]] = []
                RecordingLLM.instances.append(self)

            async def generate_text(
                self,
                prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ) -> str:
                self.calls.append({"method": "generate_text", "model_edition": self.model_edition, "prompt": prompt})
                return json.dumps({"nodes": [{"node_id": "answer", "capability_id": "main_agent.respond"}]}, ensure_ascii=False)

            async def generate_text_with_thinking(
                self,
                prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ):
                self.calls.append({"method": "generate_text_with_thinking", "model_edition": self.model_edition, "prompt": prompt})
                if "受边界约束的高层工作流规划器" in prompt:
                    yield {
                        "answer": json.dumps(
                            {"nodes": [{"node_id": "answer", "capability_id": "main_agent.respond"}]},
                            ensure_ascii=False,
                        ),
                        "reasoning": None,
                    }
                    return
                yield {"answer": "已使用所选模型。", "reasoning": None}

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
                return {
                    "provider": "fake",
                    "model": self.model_edition,
                    "config_source": config_source,
                    "reasoning_effort": reasoning_effort,
                }

        await self.reconfigure_runtime(
            main_agent_llm_config=self._model_config(),
            main_agent_llm_client_factory=RecordingLLM,
            enable_llm_planner=True,
        )
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-model-selected",
                "content": "用 flash 回答",
                "routing_mode": "auto",
                "capability_id": None,
                "model_edition": "deepseek-v4-flash-260425",
                "metadata": {},
            },
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual([client.model_edition for client in RecordingLLM.instances], ["deepseek-v4-flash-260425"])
        self.assertGreaterEqual(len(RecordingLLM.instances[0].calls), 2)
        self.assertTrue(all(call["model_edition"] == "deepseek-v4-flash-260425" for call in RecordingLLM.instances[0].calls))

        events = await self.runtime.storage.list_events_for_task(task_id)
        accepted = next(event for event in events if event.event_type == "task.accepted")
        self.assertEqual(accepted.payload["model_edition"], "deepseek-v4-flash-260425")
        llm_call = next(event for event in events if event.event_type == "main_agent.llm_call")
        self.assertEqual(llm_call.payload["model_edition"], "deepseek-v4-flash-260425")

    async def test_selected_model_edition_controls_runtime_trim_budget(self) -> None:
        class RecordingLLM:
            instances: list["RecordingLLM"] = []

            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs
                self.model_edition = kwargs["config"]["model_edition"]
                self.trim_max_tokens = kwargs["config"].get("trim_max_tokens")
                RecordingLLM.instances.append(self)

            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal") -> str:
                return json.dumps({"nodes": [{"node_id": "answer", "capability_id": "main_agent.respond"}]}, ensure_ascii=False)

            async def generate_text_with_thinking(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal"):
                yield {"answer": "已使用所选模型预算。", "reasoning": None}

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict[str, Any]:
                return {"provider": "fake", "model": self.model_edition, "trim_max_tokens": self.trim_max_tokens}

        config = self._model_config()
        config["model_editions"]["options"][0]["trim_max_tokens"] = 1000
        config["model_editions"]["options"][1]["trim_max_tokens"] = 2000
        await self.reconfigure_runtime(
            main_agent_llm_config=config,
            main_agent_llm_client_factory=RecordingLLM,
            enable_llm_planner=True,
        )
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-model-trim",
                "content": "用 pro 回答",
                "routing_mode": "auto",
                "capability_id": None,
                "model_edition": "deepseek-v4-pro-260425",
                "metadata": {},
            },
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual([client.trim_max_tokens for client in RecordingLLM.instances], [2000])

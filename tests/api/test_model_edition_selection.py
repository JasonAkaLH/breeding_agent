from __future__ import annotations

import json
from typing import Any

from tests.api.support import APITestCase


class ModelEditionSelectionAPITest(APITestCase):
    @staticmethod
    def _deepseek_reasoning() -> dict[str, Any]:
        return {
            "default": "minimal",
            "disabled_default": "minimal",
            "options": [
                {"value": "minimal", "label": "最低", "allow_when_thinking_disabled": True},
                {"value": "high", "label": "高", "allow_when_thinking_disabled": False},
                {"value": "max", "label": "最高", "allow_when_thinking_disabled": False},
            ],
        }

    @staticmethod
    def _doubao_reasoning() -> dict[str, Any]:
        return {
            "default": "minimal",
            "disabled_default": "minimal",
            "options": [
                {"value": "minimal", "label": "最低", "allow_when_thinking_disabled": True},
                {"value": "low", "label": "低", "allow_when_thinking_disabled": False},
                {"value": "medium", "label": "中", "allow_when_thinking_disabled": False},
                {"value": "high", "label": "高", "allow_when_thinking_disabled": False},
            ],
        }

    def _model_config(self) -> dict[str, Any]:
        agent_capabilities = {
            "supports_messages": True,
            "roles": ["system", "developer", "user", "assistant", "tool"],
            "supports_native_tools": True,
            "supports_required_tool_choice": True,
            "supports_streamed_tool_calls": True,
        }
        return {
            "api_key": "test",
            "base_url": "http://example.test",
            "tokenization": {"enabled": False},
            "model_editions": {
                "default": "deepseek-v4-flash-260425",
                "options": [
                    {
                        "value": "deepseek-v4-flash-260425",
                        "label": "DeepSeek V4 Flash",
                        "trim_max_tokens": 1024000,
                        "reasoning_efforts": self._deepseek_reasoning(),
                        "agent_capabilities": agent_capabilities,
                    },
                    {
                        "value": "deepseek-v4-pro-260425",
                        "label": "DeepSeek V4 Pro",
                        "trim_max_tokens": 1024000,
                        "reasoning_efforts": self._deepseek_reasoning(),
                        "agent_capabilities": agent_capabilities,
                    },
                    {
                        "value": "doubao-seed-2-1-pro-260628",
                        "label": "豆包Seed 2.1 Pro",
                        "trim_max_tokens": 256000,
                        "reasoning_efforts": self._doubao_reasoning(),
                        "agent_capabilities": agent_capabilities,
                    },
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
                    {
                        "value": "deepseek-v4-flash-260425",
                        "label": "DeepSeek V4 Flash",
                        "reasoning_efforts": self._deepseek_reasoning(),
                    },
                    {
                        "value": "deepseek-v4-pro-260425",
                        "label": "DeepSeek V4 Pro",
                        "reasoning_efforts": self._deepseek_reasoning(),
                    },
                    {
                        "value": "doubao-seed-2-1-pro-260628",
                        "label": "豆包Seed 2.1 Pro",
                        "reasoning_efforts": self._doubao_reasoning(),
                    },
                ],
            },
        )

    async def test_submit_message_rejects_disabled_disallowed_reasoning_effort(self) -> None:
        await self.reconfigure_runtime(main_agent_llm_config=self._model_config())

        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-model-invalid-effort",
                "content": "你好",
                "routing_mode": "auto",
                "capability_id": None,
                "model_edition": "doubao-seed-2-1-pro-260628",
                "metadata": {"deep_thinking": False, "main_agent_reasoning_effort": "high"},
            },
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("does not allow reasoning_effort=high", response.text)

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

    async def test_runtime_rejects_configured_model_without_reasoning_efforts(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing reasoning_efforts"):
            self.build_runtime(
                main_agent_llm_config={
                    "api_key": "test",
                    "base_url": "http://example.test",
                    "model": "legacy-model-without-reasoning-config",
                },
                enable_conversation_memory=False,
            )

    async def test_runtime_rejects_model_edition_option_without_reasoning_efforts(self) -> None:
        config = self._model_config()
        config["model_editions"]["options"][0].pop("reasoning_efforts")
        with self.assertRaisesRegex(ValueError, "missing reasoning_efforts"):
            self.build_runtime(main_agent_llm_config=config, enable_conversation_memory=False)

    async def test_endpoint_filters_non_default_non_agent_ready_edition(self) -> None:
        config = self._model_config()
        config["model_editions"]["options"][1].pop("agent_capabilities")
        await self.reconfigure_runtime(main_agent_llm_config=config)

        response = await self.client.get("/api/v1/config/model-editions")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [option["value"] for option in response.json()["options"]],
            ["deepseek-v4-flash-260425", "doubao-seed-2-1-pro-260628"],
        )

    async def test_runtime_fails_closed_when_default_is_not_agent_ready(self) -> None:
        config = self._model_config()
        config["model_editions"]["options"][0].pop("agent_capabilities")
        with self.assertRaisesRegex(ValueError, "Default model edition is not Agent-ready"):
            self.build_runtime(main_agent_llm_config=config, enable_conversation_memory=False)

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

    async def test_interrupt_resume_preserves_frontend_model_and_reasoning_metadata(self) -> None:
        skill_root = self.workspace / "skill-model-resume"
        skill = skill_root / "field-design"
        (skill / "scripts").mkdir(parents=True)
        (skill / "schemas").mkdir()
        (skill / "SKILL.md").write_text("---\nname: field-design\ndescription: design\n---\n\n# Design\n", encoding="utf-8")
        (skill / "skill.contract.yaml").write_text(
            """
contract_version: '2'
capability: {id: skill.field_design, display_name: Field Design}
runtime: {mode: python_subprocess, answer_mode: requires_finalizer}
entrypoints: {run: {path: scripts/run.py}}
input_schemas:
  diagonal: {path: schemas/diagonal.input.yaml, aliases: [diagonal, 对角线], entrypoint: run}
""",
            encoding="utf-8",
        )
        (skill / "schemas" / "diagonal.input.yaml").write_text(
            """
schema_id: diagonal
inputs:
  design: {type: string, required: true, const: diagonal, aliases: [diagonal, 对角线]}
  ncols: {type: integer, required: true, aliases: [ncols, 列数], validation: {min: 1}}
""",
            encoding="utf-8",
        )
        (skill / "scripts" / "run.py").write_text(
            "import json, sys\npayload=json.load(sys.stdin)\nprint(json.dumps({'answer':'skill ok','ncols':payload.get('ncols')}, ensure_ascii=False))\n",
            encoding="utf-8",
        )

        main_agent_calls: list[dict[str, Any]] = []

        async def main_agent_streamer(
            _prompt: str,
            *,
            stage: str | None = None,
            thinking: bool = False,
            reasoning_effort: str = "minimal",
            model_edition: str | None = None,
            **_kwargs: Any,
        ):
            main_agent_calls.append(
                {
                    "stage": stage or "final",
                    "thinking": thinking,
                    "reasoning_effort": reasoning_effort,
                    "model_edition": model_edition,
                }
            )
            if stage == "soft_skill_decision":
                yield json.dumps(
                    {
                        "decision": "execute",
                        "target_capability_id": "skill.field_design",
                        "confidence": 0.99,
                        "reason_code": "test_execute",
                    },
                    ensure_ascii=False,
                )
                return
            if thinking:
                yield {"answer": None, "reasoning": "保持深度思考。"}
            yield {"answer": "最终回答。", "reasoning": None}

        async def slot_generator(prompt: str, **kwargs: Any) -> str:
            metadata = dict(kwargs.get("metadata") or {})
            self.assertEqual(metadata.get("model_edition"), "deepseek-v4-pro-260425")
            self.assertEqual(metadata.get("main_agent_reasoning_effort"), "max")
            self.assertTrue(metadata.get("deep_thinking"))
            if '"mode": "interrupt_turn_understanding"' in prompt:
                return json.dumps({"intent": "slot_answer", "confidence": 0.99, "reason": "numeric slot value"}, ensure_ascii=False)
            if '"mode": "interrupt_resume_verification"' in prompt:
                return json.dumps({"allow_resume": True, "confidence": 0.99, "reason": "numeric slot value"}, ensure_ascii=False)
            if '"mode": "normal_extraction"' in prompt:
                return json.dumps({"resolved": {"ncols": {"raw_value": "12列", "value": "12列", "source": "current_answer"}}}, ensure_ascii=False)
            return "{}"

        await self.reconfigure_runtime(
            main_agent_llm_config=self._model_config(),
            skill_roots=(skill_root,),
            public_skill_roots=(skill_root,),
            main_agent_stream_generator=main_agent_streamer,
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-model-interrupt-resume",
                "content": "做对角线设计",
                "routing_mode": "force_capability",
                "capability_id": "main_agent.respond",
                "model_edition": "deepseek-v4-pro-260425",
                "metadata": {
                    "deep_thinking": True,
                    "main_agent_reasoning_effort": "max",
                    "forced_by_slash_command": True,
                    "slash_command": "/field-design",
                    "soft_skill_binding": {"capability_id": "skill.field_design", "command": "/field-design"},
                },
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]

        answer = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-model-interrupt-resume",
                "content": "12列",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-model-resume-answer",
                "model_edition": "deepseek-v4-pro-260425",
                "metadata": {
                    "interrupt_id": interrupt["interrupt_id"],
                    "deep_thinking": True,
                    "main_agent_reasoning_effort": "max",
                },
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        final_call = next(call for call in reversed(main_agent_calls) if call["stage"] == "final")
        self.assertEqual(final_call["model_edition"], "deepseek-v4-pro-260425")
        self.assertTrue(final_call["thinking"])
        self.assertEqual(final_call["reasoning_effort"], "max")

        events = await self.runtime.storage.list_events_for_task(task_id)
        accepted = next(event for event in events if event.event_type == "task.accepted")
        self.assertEqual(accepted.payload["model_edition"], "deepseek-v4-pro-260425")
        self.assertTrue(accepted.payload["deep_thinking"])
        llm_call = next(event for event in events if event.event_type == "main_agent.llm_call")
        self.assertEqual(llm_call.payload["model_edition"], "deepseek-v4-pro-260425")
        self.assertTrue(llm_call.payload["thinking_enabled"])
        self.assertEqual(llm_call.payload["reasoning_effort"], "max")

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

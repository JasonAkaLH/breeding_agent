from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from openai import AsyncOpenAI


ReasoningEffort = Literal["minimal", "low", "medium", "high"]

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"


def load_config(config_path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    path = Path(config_path)
    with path.open(encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict):
        raise ValueError(f"LLM config must be a mapping: {path}")
    return config


class LLMClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int | None = None,
        timeout: int | float | None = None,
        *,
        model: str | None = None,
        temperature: float | None = None,
        config: Mapping[str, Any] | None = None,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
    ) -> None:
        loaded_config = dict(config) if config is not None else load_config(config_path)

        self.model = model or loaded_config.get("model_edition") or loaded_config.get("model")
        self.temperature = temperature if temperature is not None else loaded_config.get("temperature", 0.0)
        api_key = api_key or loaded_config.get("api_key")
        base_url = base_url or loaded_config.get("base_url")
        max_retries = max_retries if max_retries is not None else loaded_config.get("max_retries", 3)
        timeout = timeout if timeout is not None else loaded_config.get("timeout", 30)
        self._base_url_configured = bool(base_url)

        missing = [
            name
            for name, value in (
                ("api_key", api_key),
                ("base_url", base_url),
                ("model", self.model),
            )
            if value in (None, "")
        ]
        if missing:
            raise ValueError(f"Missing LLM config values: {', '.join(missing)}")

        self.client = AsyncOpenAI(
            api_key=str(api_key),
            base_url=str(base_url),
            max_retries=int(max_retries),
            timeout=float(timeout),
        )

    def safe_metadata(
        self,
        *,
        config_source: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "provider": "openai_compatible",
            "model": self.model,
            "temperature": self.temperature,
            "base_url_configured": self._base_url_configured,
        }
        if config_source:
            metadata["config_source"] = config_source
        if reasoning_effort:
            metadata["reasoning_effort"] = reasoning_effort
        return metadata

    async def generate_text(
        self,
        prompt: str,
        *,
        thinking: bool = False,
        reasoning_effort: ReasoningEffort = "high",
    ) -> str:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            temperature=self.temperature,
            extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}},
            reasoning_effort=reasoning_effort,
        )
        if not response.choices:
            return ""
        content = response.choices[0].message.content
        return str(content or "")

    async def stream_text(
        self,
        prompt: str,
        *,
        reasoning_effort: ReasoningEffort = "minimal",
    ) -> AsyncIterator[str]:
        async for event in self.generate_text_with_thinking(
            prompt,
            thinking=False,
            reasoning_effort=reasoning_effort,
        ):
            answer = event.get("answer")
            if answer:
                yield answer

    async def generate_text_with_thinking(
        self,
        prompt: str,
        thinking: bool = False,
        reasoning_effort: ReasoningEffort = "high",
    ) -> AsyncIterator[dict[str, str | None]]:
        extra_body = {"thinking": {"type": "enabled" if thinking else "disabled"}}
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
            temperature=self.temperature,
            extra_body=extra_body,
            reasoning_effort=reasoning_effort,
        )

        async for chunk in stream:
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None)
            answer = delta.content

            if reasoning:
                yield {"answer": None, "reasoning": reasoning}
            if answer:
                yield {"answer": answer, "reasoning": None}

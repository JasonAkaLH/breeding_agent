from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from openai import AsyncOpenAI

from .model_editions import default_model_edition


ReasoningEffort = Literal["minimal", "high", "max"]

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"
CONFIG_ENV_PREFIX = "MAF_CONFIG_"
CONFIG_ENV_LOADED_KEY = f"{CONFIG_ENV_PREFIX}LOADED"
CONFIG_ENV_SOURCE_KEY = f"{CONFIG_ENV_PREFIX}SOURCE"
_CONFIG_ENV_CONTROL_KEYS = {CONFIG_ENV_LOADED_KEY, CONFIG_ENV_SOURCE_KEY}


def bootstrap_config_env(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    override: bool = False,
    strict: bool = True,
) -> dict[str, Any]:
    """Load YAML config once at startup and expose it as process env vars.

    Runtime consumers should call :func:`load_config` without a path so they read
    the already bootstrapped environment instead of reopening ``config.yaml``.
    Passing a path here is reserved for startup/bootstrap code and explicit
    smoke-test entry points.
    """

    path = Path(config_path)
    resolved_path = path.resolve()
    if not path.exists():
        if strict:
            raise FileNotFoundError(f"LLM config file not found: {path}")
        return load_config()

    same_source_loaded = (
        os.environ.get(CONFIG_ENV_LOADED_KEY) == "1"
        and os.environ.get(CONFIG_ENV_SOURCE_KEY) == str(resolved_path)
    )
    if same_source_loaded and not override:
        return load_config()

    previous_source = os.environ.get(CONFIG_ENV_SOURCE_KEY)
    should_clear_existing = override or (
        os.environ.get(CONFIG_ENV_LOADED_KEY) == "1"
        and previous_source is not None
        and previous_source != str(resolved_path)
    )

    with path.open(encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict):
        raise ValueError(f"LLM config must be a mapping: {path}")

    if should_clear_existing:
        _clear_config_data_env()
    for suffix, value in _iter_config_env_items(config):
        env_key = f"{CONFIG_ENV_PREFIX}{suffix}"
        if override or env_key not in os.environ:
            os.environ[env_key] = _serialize_env_value(value)
    os.environ[CONFIG_ENV_LOADED_KEY] = "1"
    os.environ[CONFIG_ENV_SOURCE_KEY] = str(resolved_path)
    return dict(config)


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Return LLM config from environment.

    ``config_path`` is kept as a compatibility/startup seam. When supplied, it
    delegates to :func:`bootstrap_config_env`, which avoids rereading an already
    bootstrapped source. Normal runtime code should call this function with no
    arguments.
    """

    if config_path is not None:
        bootstrap_config_env(config_path)
    return _load_config_from_env()


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
        config_path: str | Path | None = None,
    ) -> None:
        if config is not None:
            loaded_config = dict(config)
        else:
            if config_path is not None:
                bootstrap_config_env(config_path, override=True)
            loaded_config = load_config()

        self.model = model or _resolve_model_from_config(loaded_config)
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
        reasoning_effort: ReasoningEffort = "minimal",
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
        thinking: bool = False,
    ) -> AsyncIterator[str]:
        async for event in self.generate_text_with_thinking(
            prompt,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
        ):
            answer = event.get("answer")
            if answer:
                yield answer

    async def generate_text_with_thinking(
        self,
        prompt: str,
        thinking: bool = False,
        reasoning_effort: ReasoningEffort = "minimal",
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


def _resolve_model_from_config(config: Mapping[str, Any]) -> Any:
    selected_edition = config.get("_selected_model_edition")
    if selected_edition:
        return selected_edition
    return default_model_edition(config) or config.get("model_edition") or config.get("model")


def _iter_config_env_items(config: Mapping[str, Any], prefix: str = ""):
    for key, value in config.items():
        suffix = _to_env_suffix(str(key))
        full_suffix = f"{prefix}__{suffix}" if prefix else suffix
        if isinstance(value, Mapping):
            yield from _iter_config_env_items(value, full_suffix)
        else:
            yield full_suffix, value


def _to_env_suffix(key: str) -> str:
    return "".join(char.upper() if char.isalnum() else "_" for char in key)


def _serialize_env_value(value: Any) -> str:
    if value is None:
        return "null"
    return str(value)


def _load_config_from_env() -> dict[str, Any]:
    config: dict[str, Any] = {}
    for env_key, raw_value in os.environ.items():
        if not env_key.startswith(CONFIG_ENV_PREFIX):
            continue
        if env_key in _CONFIG_ENV_CONTROL_KEYS:
            continue
        suffix = env_key[len(CONFIG_ENV_PREFIX) :]
        if not suffix:
            continue
        _assign_nested_config_value(config, suffix, _deserialize_env_value(raw_value))
    return config


def _clear_config_data_env() -> None:
    for env_key in list(os.environ):
        if env_key.startswith(CONFIG_ENV_PREFIX) and env_key not in _CONFIG_ENV_CONTROL_KEYS:
            del os.environ[env_key]


def _assign_nested_config_value(config: dict[str, Any], suffix: str, value: Any) -> None:
    parts = [part.lower() for part in suffix.split("__") if part]
    if not parts:
        return
    current = config
    for part in parts[:-1]:
        next_value = current.setdefault(part, {})
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value


def _deserialize_env_value(raw_value: str) -> Any:
    if raw_value == "":
        return ""
    try:
        return yaml.safe_load(raw_value)
    except yaml.YAMLError:
        return raw_value

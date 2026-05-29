from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any

from .llm_client import LLMClient, ReasoningEffort, load_config
from .model_editions import config_with_model_edition, default_model_edition
from .provider_cache import provider_cache_capabilities_metadata
from src.orchestration.prompt_envelope import LLMMessage, PromptEnvelope, render_prompt_envelope

RuntimeReasoningRecorder = Callable[[str], Awaitable[None]]
PromptInput = str | PromptEnvelope | Sequence[LLMMessage | Mapping[str, Any]]


class SharedLLMRuntime:
    """Lazy single-client LLM runtime for one logical owner.

    The runtime owns one lazily-created client instance. Callers may share that
    instance across phases inside the same owner domain (for example the main
    agent's plan/observe/replan/final-answer loop). Capability-internal domains
    that need LLM help should receive a narrow adapter from the owning runtime
    instead of creating independent clients unless a product contract explicitly
    requires isolation.
    """

    def __init__(
        self,
        *,
        client_factory: Callable[..., Any] = LLMClient,
        client: Any | None = None,
        config: Mapping[str, Any] | None = None,
        config_source: str = "environment",
    ) -> None:
        self._client_factory = client_factory
        self._client = client
        self._clients_by_model_edition: dict[str, Any] = {}
        self._config = dict(config) if config is not None else None
        self._config_source = config_source
        self.runtime_id = f"llm-runtime-{id(self):x}"

    def static_metadata(
        self,
        *,
        reasoning_effort: ReasoningEffort | None = None,
        model_edition: str | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "provider": "shared_llm_runtime",
            "config_source": self._config_source,
            "llm_runtime_id": self.runtime_id,
        }
        config = self._config
        if config is None:
            try:
                config = load_config()
            except Exception:
                config = None
        if config is not None:
            model = model_edition or default_model_edition(config) or config.get("model_edition") or config.get("model")
            if model:
                metadata["model"] = model
            role_capabilities = _role_capabilities_metadata(config)
            if role_capabilities:
                metadata["provider_role_capabilities"] = role_capabilities
            cache_capabilities = provider_cache_capabilities_metadata(config)
            if cache_capabilities:
                metadata["provider_cache_capabilities"] = cache_capabilities
        if reasoning_effort:
            metadata["reasoning_effort"] = reasoning_effort
        return metadata

    @property
    def client(self) -> Any:
        if self._client is None:
            kwargs: dict[str, Any] = {}
            if self._config is not None:
                kwargs["config"] = dict(self._config)
            self._client = self._client_factory(**kwargs)
        return self._client

    def client_for_model_edition(self, model_edition: str | None = None) -> Any:
        if not model_edition:
            return self.client
        if self._client is not None and self._config is None:
            return self._client
        cached = self._clients_by_model_edition.get(model_edition)
        if cached is not None:
            return cached
        config = self._config if self._config is not None else load_config()
        client = self._client_factory(config=config_with_model_edition(config, model_edition))
        self._clients_by_model_edition[model_edition] = client
        return client

    def safe_metadata(
        self,
        *,
        reasoning_effort: ReasoningEffort | None = None,
        model_edition: str | None = None,
    ) -> dict[str, Any]:
        client = self.client_for_model_edition(model_edition)
        metadata_provider = getattr(client, "safe_metadata", None)
        if callable(metadata_provider):
            metadata = dict(metadata_provider(config_source=self._config_source, reasoning_effort=reasoning_effort))
        else:
            metadata = {"provider": type(client).__name__, "config_source": self._config_source}
            if reasoning_effort:
                metadata["reasoning_effort"] = reasoning_effort
        metadata["llm_runtime_id"] = self.runtime_id
        return metadata

    async def generate_text(
        self,
        prompt: PromptInput,
        *,
        thinking: bool = False,
        reasoning_effort: ReasoningEffort = "minimal",
        model_edition: str | None = None,
        on_reasoning_delta: RuntimeReasoningRecorder | None = None,
    ) -> str:
        if thinking and on_reasoning_delta is not None:
            chunks: list[str] = []
            async for event in self.stream_events(
                prompt,
                thinking=thinking,
                reasoning_effort=reasoning_effort,
                model_edition=model_edition,
            ):
                reasoning = event.get("reasoning")
                if reasoning:
                    await on_reasoning_delta(reasoning)
                answer = event.get("answer")
                if answer:
                    chunks.append(answer)
            return "".join(chunks)

        client = self.client_for_model_edition(model_edition)
        generate_text = getattr(client, "generate_text", None)
        if not callable(generate_text):
            raise TypeError("LLM runtime client must provide generate_text(prompt, ...).")
        result = generate_text(
            _runtime_prompt_for_client(prompt, client),
            thinking=thinking,
            reasoning_effort=reasoning_effort,
        )
        if inspect.isawaitable(result):
            result = await result
        return _coerce_text_result(result)

    async def stream_events(
        self,
        prompt: PromptInput,
        *,
        thinking: bool = False,
        reasoning_effort: ReasoningEffort = "minimal",
        model_edition: str | None = None,
    ) -> AsyncIterator[dict[str, str | None]]:
        client = self.client_for_model_edition(model_edition)
        generator = getattr(client, "generate_text_with_thinking", None)
        if not callable(generator):
            generator = getattr(client, "stream_text", None)
        if not callable(generator):
            text = await self.generate_text(
                prompt,
                thinking=thinking,
                reasoning_effort=reasoning_effort,
                model_edition=model_edition,
            )
            if text:
                yield {"answer": text, "reasoning": None}
            return

        options = _accepted_options(generator, {"thinking": thinking, "reasoning_effort": reasoning_effort})
        produced = generator(_runtime_prompt_for_client(prompt, client), **options) if options else generator(_runtime_prompt_for_client(prompt, client))
        async for event in _iter_stream_like(produced):
            coerced = _coerce_stream_event(event)
            if coerced:
                yield coerced



async def _iter_stream_like(value: Any) -> AsyncIterator[Any]:
    if hasattr(value, "__aiter__"):
        async for item in value:
            yield item
        return
    if inspect.isawaitable(value):
        yield await value
        return
    if isinstance(value, str | Mapping):
        yield value
        return
    for item in value:
        yield item


def _coerce_text_result(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        for key in ("answer", "content", "delta", "text"):
            candidate = value.get(key)
            if candidate is not None:
                return str(candidate or "")
        return ""
    return str(value or "")


def _coerce_stream_event(value: Any) -> dict[str, str | None] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        answer = _optional_string(value.get("answer") if "answer" in value else value.get("delta"))
        reasoning = _optional_string(value.get("reasoning") if "reasoning" in value else value.get("reasoning_content"))
        if answer is None and reasoning is None:
            return None
        return {"answer": answer, "reasoning": reasoning}
    text = str(value)
    if not text:
        return None
    return {"answer": text, "reasoning": None}


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _accepted_options(generator: Callable[..., Any], options: Mapping[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(generator)
    except (TypeError, ValueError):
        return {}
    accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
    return {key: value for key, value in options.items() if value is not None and (accepts_kwargs or key in signature.parameters)}


def _runtime_prompt_for_client(prompt: PromptInput, client: Any) -> PromptInput | str:
    if isinstance(prompt, PromptEnvelope) and not isinstance(client, LLMClient):
        return render_prompt_envelope(prompt).prompt
    return prompt


def _role_capabilities_metadata(config: Mapping[str, Any]) -> dict[str, Any]:
    raw = (
        config.get("provider_role_capabilities")
        or config.get("llm_role_capabilities")
        or config.get("message_role_capabilities")
        or config.get("messages")
    )
    if isinstance(raw, Mapping):
        roles = raw.get("roles") or raw.get("supported_roles") or raw.get("message_roles") or raw.get("supported_message_roles")
        return {
            "supports_messages": raw.get("supports_messages", raw.get("messages_supported", True)),
            **({"roles": list(roles)} if isinstance(roles, list | tuple) else {"roles": roles} if isinstance(roles, str) else {}),
        }
    for key in ("supported_message_roles", "message_roles"):
        roles = config.get(key)
        if isinstance(roles, list | tuple | str):
            return {"supports_messages": True, "roles": roles}
    return {}

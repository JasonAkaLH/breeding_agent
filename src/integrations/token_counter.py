from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx
import tiktoken

from src.core.errors import ModelUnavailableError
from src.integrations.llm_client import load_config
from src.integrations.model_editions import default_model_edition

_DEFAULT_ENCODING_NAME = "cl100k_base"
_TOKENIZATION_ENDPOINT_SUFFIX = "/tokenization"
_TOKENIZATION_CACHE_MAX_ITEMS = 2048
_TOKENIZATION_CACHE: OrderedDict[tuple[str, str, str], int] = OrderedDict()


class TokenizationError(ModelUnavailableError):
    """Raised when provider tokenization is required but unavailable."""


@dataclass(frozen=True, slots=True)
class ProviderTokenization:
    total_tokens: int
    offset_mapping: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class TokenBoundedText:
    text: str
    total_tokens: int
    truncated: bool
    cutoff: int


@dataclass(frozen=True, slots=True)
class _TokenizationSettings:
    enabled: bool
    fallback_to_tiktoken: bool
    api_key: str | None
    base_url: str | None
    model: str | None
    timeout: float

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.api_key and self.base_url and self.model)


@lru_cache(maxsize=None)
def _get_encoding(encoding_name: str):
    return tiktoken.get_encoding(encoding_name)


def get_num_of_tokens_from_text(
    text: str,
    *,
    encoding_name: str = _DEFAULT_ENCODING_NAME,
    config: Mapping[str, Any] | None = None,
    provider_required: bool = False,
) -> int:
    """计算单段文本的 token 数量。优先使用 provider /tokenization 的 total_tokens。"""
    return get_num_of_tokens_from_messages(
        [text],
        encoding_name=encoding_name,
        config=config,
        provider_required=provider_required,
    )


def get_num_of_tokens_from_messages(
    messages: Sequence[str],
    *,
    encoding_name: str = _DEFAULT_ENCODING_NAME,
    config: Mapping[str, Any] | None = None,
    provider_required: bool = False,
) -> int:
    """计算消息列表的 token 数量，用于裁剪上下文。"""
    texts = [str(message) for message in messages]
    settings = _resolve_tokenization_settings(config)
    if not settings.available:
        if provider_required:
            raise TokenizationError("provider tokenization is unavailable")
        return _fallback_num_tokens_from_messages(texts, encoding_name=encoding_name)
    try:
        return sum(_remote_token_counts_sync(texts, settings))
    except Exception as exc:
        if provider_required or not settings.fallback_to_tiktoken:
            raise TokenizationError("provider tokenization failed") from exc
    return _fallback_num_tokens_from_messages(texts, encoding_name=encoding_name)


async def get_num_of_tokens_from_text_async(
    text: str,
    *,
    encoding_name: str = _DEFAULT_ENCODING_NAME,
    config: Mapping[str, Any] | None = None,
    provider_required: bool = False,
) -> int:
    """Async variant for event-loop hot paths."""
    return await get_num_of_tokens_from_messages_async(
        [text],
        encoding_name=encoding_name,
        config=config,
        provider_required=provider_required,
    )


async def get_num_of_tokens_from_messages_async(
    messages: Sequence[str],
    *,
    encoding_name: str = _DEFAULT_ENCODING_NAME,
    config: Mapping[str, Any] | None = None,
    provider_required: bool = False,
) -> int:
    """Async message token counting; batches provider /tokenization calls."""
    texts = [str(message) for message in messages]
    settings = _resolve_tokenization_settings(config)
    if not settings.available:
        if provider_required:
            raise TokenizationError("provider tokenization is unavailable")
        return _fallback_num_tokens_from_messages(texts, encoding_name=encoding_name)
    try:
        return sum(await _remote_token_counts_async(texts, settings))
    except Exception as exc:
        if provider_required or not settings.fallback_to_tiktoken:
            raise TokenizationError("provider tokenization failed") from exc
    return _fallback_num_tokens_from_messages(texts, encoding_name=encoding_name)


def tokenize_text_with_offsets(
    text: str,
    *,
    model_edition: str,
    config: Mapping[str, Any] | None = None,
) -> ProviderTokenization:
    settings = _required_tokenization_settings(config, model_edition=model_edition)
    try:
        results = _request_tokenizations_sync([str(text)], settings)
    except TokenizationError:
        raise
    except Exception as exc:
        raise TokenizationError("provider tokenization failed") from exc
    if len(results) != 1:
        raise TokenizationError("provider tokenization returned a mismatched item count")
    return results[0]


async def tokenize_text_with_offsets_async(
    text: str,
    *,
    model_edition: str,
    config: Mapping[str, Any] | None = None,
) -> ProviderTokenization:
    settings = _required_tokenization_settings(config, model_edition=model_edition)
    try:
        results = await _request_tokenizations_async([str(text)], settings)
    except TokenizationError:
        raise
    except Exception as exc:
        raise TokenizationError("provider tokenization failed") from exc
    if len(results) != 1:
        raise TokenizationError("provider tokenization returned a mismatched item count")
    return results[0]


async def truncate_text_to_token_budget_async(
    text: str,
    *,
    max_tokens: int,
    model_edition: str,
    config: Mapping[str, Any] | None = None,
) -> TokenBoundedText:
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    source = str(text)
    tokenization = await tokenize_text_with_offsets_async(
        source,
        model_edition=model_edition,
        config=config,
    )
    if tokenization.total_tokens <= max_tokens:
        return TokenBoundedText(
            text=source,
            total_tokens=tokenization.total_tokens,
            truncated=False,
            cutoff=len(source),
        )
    if len(tokenization.offset_mapping) <= max_tokens:
        raise TokenizationError("provider tokenization response missing cutoff offset")
    cutoff = tokenization.offset_mapping[max_tokens][0]
    return TokenBoundedText(
        text=source[:cutoff],
        total_tokens=tokenization.total_tokens,
        truncated=True,
        cutoff=cutoff,
    )


def _fallback_num_tokens_from_messages(messages: Sequence[str], *, encoding_name: str) -> int:
    encoding = _get_encoding(encoding_name)
    return sum(len(encoding.encode(message)) for message in messages)


def _remote_token_counts_sync(texts: Sequence[str], settings: _TokenizationSettings) -> list[int]:
    cached, missing_texts, missing_indexes = _split_cached_counts(texts, settings)
    if missing_texts:
        counts = _request_token_counts_sync(missing_texts, settings)
        _merge_remote_counts(cached, missing_indexes, missing_texts, counts, settings)
    return [int(value) for value in cached]


async def _remote_token_counts_async(texts: Sequence[str], settings: _TokenizationSettings) -> list[int]:
    cached, missing_texts, missing_indexes = _split_cached_counts(texts, settings)
    if missing_texts:
        counts = await _request_token_counts_async(missing_texts, settings)
        _merge_remote_counts(cached, missing_indexes, missing_texts, counts, settings)
    return [int(value) for value in cached]


def _split_cached_counts(texts: Sequence[str], settings: _TokenizationSettings) -> tuple[list[int | None], list[str], list[int]]:
    cached: list[int | None] = []
    missing_texts: list[str] = []
    missing_indexes: list[int] = []
    assert settings.base_url is not None and settings.model is not None
    base_url = settings.base_url.rstrip("/")
    for index, text in enumerate(texts):
        key = (base_url, settings.model, text)
        cached_value = _TOKENIZATION_CACHE.get(key)
        if cached_value is not None:
            _TOKENIZATION_CACHE.move_to_end(key)
            cached.append(cached_value)
        else:
            cached.append(None)
            missing_texts.append(text)
            missing_indexes.append(index)
    return cached, missing_texts, missing_indexes


def _merge_remote_counts(
    cached: list[int | None],
    missing_indexes: Sequence[int],
    missing_texts: Sequence[str],
    counts: Sequence[int],
    settings: _TokenizationSettings,
) -> None:
    if len(counts) != len(missing_indexes):
        raise TokenizationError("provider tokenization returned a mismatched item count")
    assert settings.base_url is not None and settings.model is not None
    base_url = settings.base_url.rstrip("/")
    for index, text, count in zip(missing_indexes, missing_texts, counts, strict=True):
        parsed = int(count)
        cached[index] = parsed
        _cache_token_count((base_url, settings.model, text), parsed)


def _cache_token_count(key: tuple[str, str, str], count: int) -> None:
    _TOKENIZATION_CACHE[key] = count
    _TOKENIZATION_CACHE.move_to_end(key)
    while len(_TOKENIZATION_CACHE) > _TOKENIZATION_CACHE_MAX_ITEMS:
        _TOKENIZATION_CACHE.popitem(last=False)


def _request_token_counts_sync(texts: Sequence[str], settings: _TokenizationSettings) -> list[int]:
    return [item.total_tokens for item in _request_tokenizations_sync(texts, settings)]


def _request_tokenizations_sync(
    texts: Sequence[str], settings: _TokenizationSettings
) -> list[ProviderTokenization]:
    assert settings.api_key is not None and settings.base_url is not None and settings.model is not None
    with httpx.Client(timeout=settings.timeout) as client:
        response = client.post(
            f"{settings.base_url.rstrip('/')}{_TOKENIZATION_ENDPOINT_SUFFIX}",
            headers=_headers(settings.api_key),
            json={"model": settings.model, "text": list(texts)},
        )
    return _parse_tokenization_response(
        response,
        expected_model=settings.model,
        texts=texts,
    )


async def _request_token_counts_async(texts: Sequence[str], settings: _TokenizationSettings) -> list[int]:
    results = await _request_tokenizations_async(texts, settings)
    return [item.total_tokens for item in results]


async def _request_tokenizations_async(
    texts: Sequence[str], settings: _TokenizationSettings
) -> list[ProviderTokenization]:
    assert settings.api_key is not None and settings.base_url is not None and settings.model is not None
    async with httpx.AsyncClient(timeout=settings.timeout) as client:
        response = await client.post(
            f"{settings.base_url.rstrip('/')}{_TOKENIZATION_ENDPOINT_SUFFIX}",
            headers=_headers(settings.api_key),
            json={"model": settings.model, "text": list(texts)},
        )
    return _parse_tokenization_response(
        response,
        expected_model=settings.model,
        texts=texts,
    )


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _parse_tokenization_response(
    response: httpx.Response,
    *,
    expected_model: str,
    texts: Sequence[str],
) -> list[ProviderTokenization]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise TokenizationError("provider tokenization returned non-JSON response") from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise TokenizationError(f"provider tokenization returned HTTP {response.status_code}")
    if not isinstance(payload, Mapping) or payload.get("model") != expected_model:
        raise TokenizationError("provider tokenization response model mismatch")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != len(texts):
        raise TokenizationError("provider tokenization response missing data list")
    parsed: list[ProviderTokenization | None] = [None] * len(texts)
    for item in data:
        if not isinstance(item, Mapping):
            raise TokenizationError("provider tokenization data item must be an object")
        index = item.get("index")
        total_tokens = item.get("total_tokens")
        token_ids = item.get("token_ids")
        offsets = item.get("offset_mapping")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= len(texts)
            or parsed[index] is not None
        ):
            raise TokenizationError("provider tokenization data item index is invalid")
        if isinstance(total_tokens, bool) or not isinstance(total_tokens, int) or total_tokens < 0:
            raise TokenizationError("provider tokenization data item missing integer total_tokens")
        if not isinstance(token_ids, list) or any(
            isinstance(token_id, bool) or not isinstance(token_id, int)
            for token_id in token_ids
        ):
            raise TokenizationError("provider tokenization data item has invalid token_ids")
        if not isinstance(offsets, list):
            raise TokenizationError("provider tokenization data item has invalid offset_mapping")
        parsed_offsets: list[tuple[int, int]] = []
        previous_start = 0
        previous_end = 0
        text_length = len(str(texts[index]))
        for offset in offsets:
            if (
                not isinstance(offset, (list, tuple))
                or len(offset) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) for value in offset)
            ):
                raise TokenizationError("provider tokenization offset must be an integer pair")
            start, end = int(offset[0]), int(offset[1])
            if (
                start < 0
                or end < start
                or end > text_length
                or start < previous_start
                or end < previous_end
            ):
                raise TokenizationError("provider tokenization offset is out of range")
            parsed_offsets.append((start, end))
            previous_start = start
            previous_end = end
        if total_tokens != len(token_ids) or total_tokens != len(parsed_offsets):
            raise TokenizationError("provider tokenization data item arrays are misaligned")
        parsed[index] = ProviderTokenization(total_tokens, tuple(parsed_offsets))
    if any(item is None for item in parsed):
        raise TokenizationError("provider tokenization response indexes are incomplete")
    return [item for item in parsed if item is not None]


def _resolve_tokenization_settings(
    config: Mapping[str, Any] | None,
    *,
    model_edition: str | None = None,
) -> _TokenizationSettings:
    loaded = dict(config) if config is not None else load_config()
    nested_raw = loaded.get("tokenization")
    nested = nested_raw if isinstance(nested_raw, Mapping) else {}
    enabled = _coerce_bool(nested.get("enabled", loaded.get("tokenization_enabled")), default=True)
    fallback_to_tiktoken = _coerce_bool(
        nested.get("fallback_to_tiktoken", loaded.get("tokenization_fallback_to_tiktoken")),
        default=True,
    )
    timeout = _coerce_positive_float(nested.get("timeout", loaded.get("tokenization_timeout")))
    if timeout is None:
        timeout = _coerce_positive_float(loaded.get("timeout")) or 30.0
    timeout = min(timeout, 10.0)
    model = _clean_text(
        model_edition
        or nested.get("model")
        or loaded.get("_selected_model_edition")
        or loaded.get("model_edition")
        or default_model_edition(loaded)
        or loaded.get("model")
    )
    return _TokenizationSettings(
        enabled=enabled,
        fallback_to_tiktoken=fallback_to_tiktoken,
        api_key=_clean_text(nested.get("api_key") or loaded.get("api_key")),
        base_url=_clean_text(nested.get("base_url") or loaded.get("base_url")),
        model=model,
        timeout=timeout,
    )


def _required_tokenization_settings(
    config: Mapping[str, Any] | None,
    *,
    model_edition: str,
) -> _TokenizationSettings:
    model = _clean_text(model_edition)
    if model is None:
        raise TokenizationError("provider tokenization model is unavailable")
    settings = _resolve_tokenization_settings(config, model_edition=model)
    if not settings.available or settings.model != model:
        raise TokenizationError("provider tokenization is unavailable")
    return settings


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_positive_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

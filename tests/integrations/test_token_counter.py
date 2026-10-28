import unittest
from unittest.mock import patch

import httpx

from src.core.errors import ModelUnavailableError
from src.integrations.token_counter import (
    ProviderTokenization,
    TokenizationError,
    TokenBoundedText,
    _TOKENIZATION_CACHE,
    _parse_tokenization_response,
    _resolve_tokenization_settings,
    get_num_of_tokens_from_messages,
    get_num_of_tokens_from_messages_async,
    get_num_of_tokens_from_text,
    tokenize_text_with_offsets,
    truncate_text_to_token_budget_async,
)


class TokenCounterTests(unittest.TestCase):
    def setUp(self) -> None:
        _TOKENIZATION_CACHE.clear()

    def test_counts_tokens_for_context_messages(self) -> None:
        self.assertEqual(get_num_of_tokens_from_messages(["hello tiktoken"]), 3)
        self.assertEqual(get_num_of_tokens_from_text("hello tiktoken"), 3)

    def test_sums_multiple_messages(self) -> None:
        combined = get_num_of_tokens_from_messages(["hello tiktoken", "上下文裁剪"])
        first = get_num_of_tokens_from_messages(["hello tiktoken"])
        second = get_num_of_tokens_from_messages(["上下文裁剪"])

        self.assertEqual(combined, first + second)
        self.assertGreater(combined, first)

    def test_uses_provider_tokenization_total_tokens_when_configured(self) -> None:
        config = {
            "api_key": "test-key",
            "base_url": "https://ark.example.test/api/v3",
            "model_edition": "deepseek-v4-flash-260425",
        }

        with patch("src.integrations.token_counter._request_token_counts_sync", return_value=[11]) as request:
            count = get_num_of_tokens_from_text("hello tiktoken", config=config)

        self.assertEqual(count, 11)
        request.assert_called_once()
        self.assertEqual(list(request.call_args.args[0]), ["hello tiktoken"])

    def test_batches_provider_tokenization_for_message_lists(self) -> None:
        config = {
            "api_key": "test-key",
            "base_url": "https://ark.example.test/api/v3",
            "model_edition": "deepseek-v4-flash-260425",
        }

        with patch("src.integrations.token_counter._request_token_counts_sync", return_value=[5, 3]) as request:
            count = get_num_of_tokens_from_messages(["hello tiktoken", "上下文裁剪"], config=config)

        self.assertEqual(count, 8)
        request.assert_called_once()
        self.assertEqual(list(request.call_args.args[0]), ["hello tiktoken", "上下文裁剪"])

    def test_falls_back_to_tiktoken_when_provider_tokenization_fails(self) -> None:
        config = {
            "api_key": "test-key",
            "base_url": "https://ark.example.test/api/v3",
            "model_edition": "deepseek-v4-flash-260425",
        }

        with patch("src.integrations.token_counter._request_token_counts_sync", side_effect=RuntimeError("network")):
            self.assertEqual(get_num_of_tokens_from_text("hello tiktoken", config=config), 3)

    def test_can_fail_closed_when_provider_tokenization_fails(self) -> None:
        config = {
            "api_key": "test-key",
            "base_url": "https://ark.example.test/api/v3",
            "model_edition": "deepseek-v4-flash-260425",
            "tokenization": {"fallback_to_tiktoken": False},
        }

        with patch("src.integrations.token_counter._request_token_counts_sync", side_effect=RuntimeError("network")):
            with self.assertRaises(TokenizationError):
                get_num_of_tokens_from_text("hello tiktoken", config=config)

    def test_provider_required_fails_closed_when_config_is_unavailable(self) -> None:
        with self.assertRaises(ModelUnavailableError):
            get_num_of_tokens_from_text(
                "hello",
                config={"tokenization": {"fallback_to_tiktoken": False}},
                provider_required=True,
            )

    def test_tokenization_timeout_is_capped_at_ten_seconds(self) -> None:
        settings = _resolve_tokenization_settings(
            {
                "api_key": "test-key",
                "base_url": "https://ark.example.test/api/v3",
                "model_edition": "deepseek-v4-flash-260425",
                "tokenization": {"timeout": 99},
            }
        )

        self.assertEqual(settings.timeout, 10.0)

    def test_parses_model_bound_offsets_and_reorders_by_index(self) -> None:
        request = httpx.Request("POST", "https://ark.example.test/api/v3/tokenization")
        response = httpx.Response(
            200,
            request=request,
            json={
                "model": "model-a",
                "data": [
                    {
                        "index": 1,
                        "total_tokens": 2,
                        "token_ids": [0, 21],
                        "offset_mapping": [[0, 0], [0, 1]],
                    },
                    {
                        "index": 0,
                        "total_tokens": 3,
                        "token_ids": [0, 11, 12],
                        "offset_mapping": [[0, 0], [0, 1], [1, 2]],
                    },
                ],
            },
        )

        parsed = _parse_tokenization_response(
            response,
            expected_model="model-a",
            texts=["中文", "🙂"],
        )

        self.assertEqual(
            parsed,
            [
                ProviderTokenization(3, ((0, 0), (0, 1), (1, 2))),
                ProviderTokenization(2, ((0, 0), (0, 1))),
            ],
        )

    def test_rejects_response_model_or_offset_contract_drift(self) -> None:
        request = httpx.Request("POST", "https://ark.example.test/api/v3/tokenization")
        cases = (
            {
                "model": "other-model",
                "data": [
                    {
                        "index": 0,
                        "total_tokens": 1,
                        "token_ids": [1],
                        "offset_mapping": [[0, 0]],
                    }
                ],
            },
            {
                "model": "model-a",
                "data": [
                    {
                        "index": 0,
                        "total_tokens": 2,
                        "token_ids": [1],
                        "offset_mapping": [[0, 0]],
                    }
                ],
            },
            {
                "model": "model-a",
                "data": [
                    {
                        "index": 0,
                        "total_tokens": 1,
                        "token_ids": [1],
                        "offset_mapping": [[0, 2]],
                    }
                ],
            },
            {
                "model": "model-a",
                "data": [
                    {
                        "index": 0,
                        "total_tokens": 2,
                        "token_ids": [1, 2],
                        "offset_mapping": [[0, 1], [0, 0]],
                    }
                ],
            },
        )
        for payload in cases:
            with self.subTest(payload=payload):
                response = httpx.Response(200, request=request, json=payload)
                with self.assertRaises(TokenizationError):
                    _parse_tokenization_response(
                        response,
                        expected_model="model-a",
                        texts=["x"],
                    )

    def test_sync_detailed_tokenization_uses_explicit_model(self) -> None:
        expected = ProviderTokenization(2, ((0, 0), (0, 1)))

        def fake_request(texts, settings):
            self.assertEqual(list(texts), ["x"])
            self.assertEqual(settings.model, "model-a")
            return [expected]

        with patch(
            "src.integrations.token_counter._request_tokenizations_sync",
            side_effect=fake_request,
        ) as request:
            actual = tokenize_text_with_offsets(
                "x",
                model_edition="model-a",
                config={
                    "api_key": "test-key",
                    "base_url": "https://ark.example.test/api/v3",
                    "model_edition": "wrong-default",
                },
            )

        request.assert_called_once()
        self.assertEqual(actual, expected)

    def test_single_request_truncates_at_first_excluded_token_start(self) -> None:
        text = "x" * 49_998 + "🙂"
        offsets = (
            ((0, 0),)
            + tuple((index, index + 1) for index in range(49_998))
            + ((49_998, 49_999), (49_998, 49_999))
        )

        async def fake_request(texts, settings):
            self.assertEqual(list(texts), [text])
            self.assertEqual(settings.model, "model-a")
            return [ProviderTokenization(50_001, offsets)]

        async def run() -> TokenBoundedText:
            with patch(
                "src.integrations.token_counter._request_tokenizations_async",
                side_effect=fake_request,
            ) as request:
                result = await truncate_text_to_token_budget_async(
                    text,
                    max_tokens=50_000,
                    model_edition="model-a",
                    config={
                        "api_key": "test-key",
                        "base_url": "https://ark.example.test/api/v3",
                    },
                )
            request.assert_awaited_once()
            return result

        self.assertEqual(
            __import__("asyncio").run(run()),
            TokenBoundedText(
                text="x" * 49_998,
                total_tokens=50_001,
                truncated=True,
                cutoff=49_998,
            ),
        )

    def test_single_request_keeps_text_within_token_budget(self) -> None:
        async def run() -> TokenBoundedText:
            with patch(
                "src.integrations.token_counter._request_tokenizations_async",
                return_value=[ProviderTokenization(3, ((0, 0), (0, 1), (1, 2)))],
            ) as request:
                result = await truncate_text_to_token_budget_async(
                    "中文",
                    max_tokens=50_000,
                    model_edition="model-a",
                    config={
                        "api_key": "test-key",
                        "base_url": "https://ark.example.test/api/v3",
                    },
                )
            request.assert_awaited_once()
            return result

        self.assertEqual(
            __import__("asyncio").run(run()),
            TokenBoundedText(
                text="中文",
                total_tokens=3,
                truncated=False,
                cutoff=2,
            ),
        )

    def test_async_provider_tokenization_uses_total_tokens(self) -> None:
        config = {
            "api_key": "test-key",
            "base_url": "https://ark.example.test/api/v3",
            "model_edition": "deepseek-v4-flash-260425",
        }

        async def fake_request(texts, settings):
            return [7, 9]

        async def run() -> int:
            with patch("src.integrations.token_counter._request_token_counts_async", side_effect=fake_request) as request:
                count = await get_num_of_tokens_from_messages_async(["a", "b"], config=config)
            self.assertEqual(request.call_count, 1)
            return count

        self.assertEqual(__import__("asyncio").run(run()), 16)


if __name__ == "__main__":
    unittest.main()

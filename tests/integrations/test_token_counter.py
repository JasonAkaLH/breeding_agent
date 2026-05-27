import unittest
from unittest.mock import patch

from src.integrations.token_counter import (
    TokenizationError,
    _TOKENIZATION_CACHE,
    get_num_of_tokens_from_messages,
    get_num_of_tokens_from_messages_async,
    get_num_of_tokens_from_text,
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

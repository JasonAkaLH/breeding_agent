import unittest

from src.integrations.token_counter import get_num_of_tokens_from_messages, get_num_of_tokens_from_text


class TokenCounterTests(unittest.TestCase):
    def test_counts_tokens_for_context_messages(self) -> None:
        self.assertEqual(get_num_of_tokens_from_messages(["hello tiktoken"]), 3)
        self.assertEqual(get_num_of_tokens_from_text("hello tiktoken"), 3)

    def test_sums_multiple_messages(self) -> None:
        combined = get_num_of_tokens_from_messages(["hello tiktoken", "上下文裁剪"])
        first = get_num_of_tokens_from_messages(["hello tiktoken"])
        second = get_num_of_tokens_from_messages(["上下文裁剪"])

        self.assertEqual(combined, first + second)
        self.assertGreater(combined, first)


if __name__ == "__main__":
    unittest.main()

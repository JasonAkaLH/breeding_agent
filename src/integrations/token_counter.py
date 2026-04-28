from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache

import tiktoken

_DEFAULT_ENCODING_NAME = "cl100k_base"


@lru_cache(maxsize=None)
def _get_encoding(encoding_name: str):
    return tiktoken.get_encoding(encoding_name)


def get_num_of_tokens_from_text(text: str, *, encoding_name: str = _DEFAULT_ENCODING_NAME) -> int:
    """计算单段文本的 token 数量。"""
    encoding = _get_encoding(encoding_name)
    return len(encoding.encode(text))


def get_num_of_tokens_from_messages(messages: Sequence[str]) -> int:
    """计算消息列表的 token 数量，用于裁剪上下文。"""
    return sum(get_num_of_tokens_from_text(message) for message in messages)

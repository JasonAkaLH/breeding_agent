from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeAlias

ConversationTitleGenerator: TypeAlias = Callable[[str], str | Awaitable[str]]

MAX_CONVERSATION_TITLE_LENGTH = 60
AUTO_CONVERSATION_TITLE_LENGTH = 24


async def call_title_generator(generator: ConversationTitleGenerator, title_source: str) -> str:
    result = generator(title_source)
    if inspect.isawaitable(result):
        result = await result
    return str(result or "")


def validate_conversation_title(title: str) -> str:
    normalized = _collapse_whitespace(title)
    if not normalized:
        raise ValueError("Conversation title cannot be empty.")
    if len(normalized) > MAX_CONVERSATION_TITLE_LENGTH:
        raise ValueError(f"Conversation title cannot exceed {MAX_CONVERSATION_TITLE_LENGTH} characters.")
    return normalized


def normalize_generated_conversation_title(raw_title: str) -> str | None:
    title = _first_non_empty_line(raw_title)
    title = _strip_label_prefix(title)
    title = title.strip(" \t\r\n`*_#\"'“”‘’《》<>:：。.!！?？,，、-—")
    title = _collapse_whitespace(title)
    if not title:
        return None
    if len(title) > AUTO_CONVERSATION_TITLE_LENGTH:
        title = title[:AUTO_CONVERSATION_TITLE_LENGTH]
    try:
        return validate_conversation_title(title)
    except ValueError:
        return None


def build_conversation_title_source(user_messages: Sequence[str]) -> str:
    normalized_messages = [_collapse_whitespace(message) for message in user_messages]
    normalized_messages = [message for message in normalized_messages if message]
    return "\n".join(
        f"用户第{index}轮：{message}"
        for index, message in enumerate(normalized_messages, start=1)
    )


def build_conversation_title_prompt(user_messages_source: str) -> str:
    return (
        "请根据用户在同一个会话中已经发过的所有消息，为这段会话生成一个简短中文名称。\n"
        "要求：\n"
        "1. 只输出名称本身，不要解释、不要编号、不要加引号。\n"
        "2. 尽量 6 到 18 个汉字；最多不超过 24 个字符。\n"
        "3. 名称要概括整个会话主题，帮助用户回忆会话内容。\n\n"
        f"用户已发送消息：\n{user_messages_source.strip()}\n"
        "会话名称："
    )


def _first_non_empty_line(value: str) -> str:
    for line in str(value).splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _strip_label_prefix(value: str) -> str:
    return re.sub(r"^(会话)?(标题|名称)\s*[:：]\s*", "", value.strip(), count=1)


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip())

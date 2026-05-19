from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx


class QueueSSEStream(httpx.AsyncByteStream):
    """Queue-backed async SSE stream for legacy HTTP+SSE transport tests."""

    def __init__(self, initial: str = "") -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False
        if initial:
            self.send_text(initial)

    def send_text(self, text: str) -> None:
        self._queue.put_nowait(text.encode("utf-8"))

    def send_message(self, payload: Mapping[str, Any]) -> None:
        self.send_text(f"event: message\ndata: {json.dumps(dict(payload), ensure_ascii=False)}\n\n")

    async def __aiter__(self):
        while True:
            item = await self._queue.get()
            if item is None:
                break
            yield item

    async def aclose(self) -> None:
        if not self.closed:
            self.closed = True
            self._queue.put_nowait(None)

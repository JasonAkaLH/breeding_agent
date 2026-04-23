from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.contracts import AuditSink, Payload


class JsonlAuditSink(AuditSink):
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def record(
        self,
        event_type: str,
        payload: Payload,
        *,
        conversation_id: str | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> None:
        record = {
            "recorded_at": self._utcnow(),
            "event_type": event_type,
            "conversation_id": conversation_id,
            "task_id": task_id,
            "node_id": node_id,
            "payload": dict(payload),
        }
        line = json.dumps(record, ensure_ascii=False, default=self._json_default) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append_line, line)

    def _append_line(self, line: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    @staticmethod
    def _json_default(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

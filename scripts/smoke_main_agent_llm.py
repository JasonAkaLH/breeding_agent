#!/usr/bin/env python3
"""Manual smoke test for the main-agent real LLM runtime binding.

This script is intentionally excluded from default unittest discovery. It uses a
local LLM config file and performs a real provider call only when invoked
explicitly by a developer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api.dto import SubmitMessageRequest
from src.api.runtime import build_api_runtime


def _json_default(value: Any) -> str:
    return str(value)


async def _wait_for_terminal_task(runtime, task_id: str, *, timeout: float) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        task = await runtime.storage.get_task(task_id)
        if task is not None and str(task.status) in {"completed", "failed", "cancelled"}:
            return task
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Task {task_id} did not finish within {timeout} seconds")
        await asyncio.sleep(0.05)


async def _run(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    if not config_path.exists():
        raise FileNotFoundError(f"LLM config file not found: {config_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        database_path = Path(args.database_path) if args.database_path else tmp / "main-agent-smoke.sqlite3"
        audit_log_path = Path(args.audit_log_path) if args.audit_log_path else tmp / "audit.jsonl"
        runtime = build_api_runtime(
            database_path=database_path,
            audit_log_path=audit_log_path,
            main_agent_llm_config_path=config_path,
            main_agent_reasoning_effort=args.reasoning_effort,
            skill_roots=[],
        )
        try:
            _message, task = await runtime.submit_message(
                args.conversation_id,
                SubmitMessageRequest(
                    account_id=args.account_id,
                    content=args.message,
                    capability_id=None,
                    metadata={},
                ),
            )
            terminal_task = await _wait_for_terminal_task(runtime, task.task_id, timeout=args.timeout)
            events = await runtime.storage.list_events_for_task(task.task_id)
            event_types = [event.event_type for event in events]
            llm_events = [event for event in events if event.event_type in {"main_agent.llm_call", "main_agent.llm_fallback"}]
            frontend_events = [event for event in events if event.event_type.startswith("main_agent.output_")]

            print(
                json.dumps(
                    {
                        "task_id": task.task_id,
                        "status": str(terminal_task.status),
                        "event_types": event_types,
                        "frontend_event_count": len(frontend_events),
                        "llm_events": [dict(event.payload) for event in llm_events],
                        "audit_log_path": str(audit_log_path),
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=_json_default,
                )
            )
            if str(terminal_task.status) != "completed":
                return 2
            if "main_agent.output_delta" not in event_types or "main_agent.output_final" not in event_types:
                return 3
            if "main_agent.llm_call" not in event_types:
                return 4
            return 0
        finally:
            await runtime.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the main-agent real LLM runtime binding.")
    parser.add_argument("--config", default="config.yaml", help="Path to local LLM config YAML. Default: config.yaml")
    parser.add_argument("--message", default="你好，请用一句话确认主代理真实 LLM 已接通。")
    parser.add_argument("--conversation-id", default="smoke-main-agent-llm")
    parser.add_argument("--account-id", default="smoke-account")
    parser.add_argument("--reasoning-effort", default="minimal", choices=["minimal", "low", "medium", "high"])
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--database-path", default=None, help="Optional SQLite path. Defaults to a temporary file.")
    parser.add_argument("--audit-log-path", default=None, help="Optional audit JSONL path. Defaults to a temporary file.")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

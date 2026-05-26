#!/usr/bin/env python
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.api.runtime import build_api_runtime  # noqa: E402
from src.core.enums import ConversationStatus  # noqa: E402
from src.state.errors import redact_text  # noqa: E402
from src.storage.postgres import PostgreSQLStorage, bootstrap_postgres_database, create_postgres_engine, create_postgres_session_factory  # noqa: E402


def _conversation_public_dict(conversation: Any) -> dict[str, Any]:
    return {
        "conversation_id": conversation.conversation_id,
        "username": conversation.username,
        "status": str(conversation.status),
        "delete_runner_id": conversation.delete_runner_id,
        "delete_requested_at": conversation.delete_requested_at,
        "delete_started_at": conversation.delete_started_at,
        "delete_finished_at": conversation.delete_finished_at,
        "delete_failed_at": conversation.delete_failed_at,
        "delete_error_code": conversation.delete_error_code,
        "delete_error_summary": redact_text(conversation.delete_error_summary),
        "delete_phase": conversation.delete_phase,
        "updated_at": conversation.updated_at,
    }


def _print(payload: dict[str, Any], *, as_json: bool, code: int) -> int:
    sanitized = _sanitize_payload(payload)
    if as_json:
        print(json.dumps(sanitized, ensure_ascii=False, sort_keys=True, default=str))
    else:
        print(sanitized)
    return code


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ("<redacted>" if any(marker in str(key).lower() for marker in ("dsn", "password", "token", "secret", "api_key", "apikey")) else _sanitize_payload(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _load_dsn(args: argparse.Namespace) -> str:
    if getattr(args, "dsn", ""):
        raise ValueError("raw DSN CLI arguments are not allowed; use --dsn-env with MAF_POSTGRES_STATE_DSN")
    dsn_env = getattr(args, "dsn_env", "MAF_POSTGRES_STATE_DSN") or "MAF_POSTGRES_STATE_DSN"
    dsn = os.environ.get(dsn_env, "").strip()
    if not dsn:
        raise ValueError(f"PostgreSQL DSN is required in {dsn_env}")
    return dsn


async def _list(args: argparse.Namespace) -> int:
    dsn = _load_dsn(args)
    engine = create_postgres_engine(dsn)
    try:
        if args.bootstrap:
            bootstrap_postgres_database(engine)
        storage = PostgreSQLStorage(create_postgres_session_factory(engine))
        conversations = await storage.list_deleting_conversations()
        filtered = [
            conversation for conversation in conversations
            if args.include_deleting or str(conversation.status) == str(ConversationStatus.DELETING_FAILED)
        ]
        return _print(
            {
                "status": "ok",
                "count": len(filtered),
                "conversations": [_conversation_public_dict(conversation) for conversation in filtered],
            },
            as_json=args.json,
            code=0,
        )
    finally:
        engine.dispose()


async def _retry(args: argparse.Namespace) -> int:
    _load_dsn(args)
    os.environ.setdefault("MAF_STATE_STORE_BACKEND", "postgresql")
    backend = os.environ.get("MAF_STATE_STORE_BACKEND", "").strip().lower()
    if backend != "postgresql":
        raise ValueError("conversation delete retry requires MAF_STATE_STORE_BACKEND=postgresql")
    runtime = build_api_runtime(
        database_path=Path(args.database_path),
        audit_log_path=Path(args.audit_log_path),
        artifact_store_path=Path(args.artifact_store_path) if args.artifact_store_path else None,
    )
    try:
        result = await runtime.retry_failed_conversation_delete(args.conversation_id)
        return _print({"status": "ok", "result": result}, as_json=args.json, code=0)
    finally:
        await runtime.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose and retry PostgreSQL strong conversation deletion safely.")
    parser.add_argument("--dsn", default="", help="Rejected compatibility shim; use --dsn-env instead.")
    parser.add_argument("--dsn-env", default="MAF_POSTGRES_STATE_DSN")
    parser.add_argument("--json", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List deleting/deleting_failed conversations with sanitized metadata.")
    list_parser.add_argument("--include-deleting", action="store_true", help="Include in-progress deleting rows as well as failed rows.")
    list_parser.add_argument("--bootstrap", action="store_true", help="Run no-drop schema bootstrap before listing.")

    retry_parser = subparsers.add_parser("retry", help="Re-enter the runtime deletion runner for a deleting_failed conversation.")
    retry_parser.add_argument("--conversation-id", required=True)
    retry_parser.add_argument("--database-path", default=os.environ.get("MAF_SQLITE_DEV_PATH", "runtime/dev.sqlite3"))
    retry_parser.add_argument("--audit-log-path", default=os.environ.get("MAF_AUDIT_LOG_PATH", "runtime/audit.jsonl"))
    retry_parser.add_argument("--artifact-store-path", default=os.environ.get("MAF_ARTIFACT_STORE_PATH", ""))

    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return asyncio.run(_list(args))
        if args.command == "retry":
            return asyncio.run(_retry(args))
    except Exception as exc:  # noqa: BLE001 - CLI must fail closed with sanitized diagnostics.
        return _print({"status": "failed", "error": str(exc)}, as_json=args.json, code=2)
    return _print({"status": "failed", "error": "unknown command"}, as_json=args.json, code=2)


if __name__ == "__main__":
    raise SystemExit(main())

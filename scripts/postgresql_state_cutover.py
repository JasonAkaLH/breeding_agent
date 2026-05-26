#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.state.cutover import FreshCutoverInput, build_postgres_fresh_cutover_plan  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a redacted PostgreSQL fresh cutover readiness plan.")
    parser.add_argument("--postgres-dsn", default="", help="Rejected compatibility shim; use --dsn-env instead.")
    parser.add_argument("--dsn-env", default="MAF_POSTGRES_STATE_DSN")
    parser.add_argument("--sqlite-path", default="", help="Rejected: SQLite import is not part of fresh cutover.")
    parser.add_argument("--schema-ready", action="store_true")
    parser.add_argument("--runtime-smoke-ready", action="store_true")
    parser.add_argument("--sqlite-history-abandoned", action="store_true")
    parser.add_argument("--queue-backlog", type=int, default=0)
    parser.add_argument("--dead-letter-count", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.postgres_dsn:
        return _print({"status": "failed", "error": "raw DSN CLI arguments are not allowed; use --dsn-env with MAF_POSTGRES_STATE_DSN"}, args.json, 2)
    if args.sqlite_path:
        return _print({"status": "failed", "error": "SQLite import is not part of PostgreSQL fresh cutover"}, args.json, 2)
    dsn = os.environ.get(args.dsn_env, "") if args.dsn_env else ""
    plan = build_postgres_fresh_cutover_plan(
        FreshCutoverInput(
            postgres_dsn=dsn,
            schema_ready=args.schema_ready,
            runtime_smoke_ready=args.runtime_smoke_ready,
            queue_backlog=args.queue_backlog,
            dead_letter_count=args.dead_letter_count,
            sqlite_history_abandoned=args.sqlite_history_abandoned,
        )
    )
    return _print({"status": "ok" if plan.ready else "blocked", "plan": plan.public_dict()}, args.json, 0 if plan.ready else 2)


def _print(payload: dict[str, Any], as_json: bool, code: int) -> int:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    else:
        print(payload)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

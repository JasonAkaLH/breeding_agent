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

from src.state.migration import build_sqlite_to_postgres_migration_plan  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a redacted SQLite to PostgreSQL State Platform migration plan.")
    parser.add_argument("--sqlite-path", required=True)
    parser.add_argument("--postgres-dsn", default="", help="Rejected compatibility shim; use --dsn-env instead.")
    parser.add_argument("--dsn-env", default="MAF_POSTGRES_STATE_DSN")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--operator-confirmation", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.postgres_dsn:
        raw_dsn_error_payload: dict[str, Any] = {
            "status": "failed",
            "error": "raw DSN CLI arguments are not allowed; use --dsn-env with MAF_POSTGRES_STATE_DSN",
        }
        if args.json:
            print(json.dumps(raw_dsn_error_payload, ensure_ascii=False, sort_keys=True))
        else:
            print(raw_dsn_error_payload["error"], file=sys.stderr)
        return 2
    postgres_dsn = os.environ.get(args.dsn_env, "") if args.dsn_env else ""
    try:
        plan = build_sqlite_to_postgres_migration_plan(
            sqlite_path=Path(args.sqlite_path),
            postgres_dsn=postgres_dsn,
            dry_run=args.dry_run,
            operator_confirmation=args.operator_confirmation,
        )
    except Exception as exc:
        error_payload: dict[str, Any] = {"status": "failed", "error": str(exc)}
        if args.json:
            print(json.dumps(error_payload, ensure_ascii=False, sort_keys=True))
        else:
            print(error_payload["error"], file=sys.stderr)
        return 2
    ok_payload: dict[str, Any] = {"status": "ok", "plan": plan.public_dict()}
    if args.json:
        print(json.dumps(ok_payload, ensure_ascii=False, sort_keys=True, default=str))
    else:
        print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

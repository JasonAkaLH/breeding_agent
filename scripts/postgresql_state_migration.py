#!/usr/bin/env python
from __future__ import annotations

"""Compatibility shim.

PostgreSQL cutover is now a fresh canonical start. SQLite data migration is
intentionally not part of this workflow; use postgresql_state_cutover.py.
"""

import argparse
import json
import sys
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deprecated SQLite migration shim for PostgreSQL fresh cutover.")
    parser.add_argument("--sqlite-path", default="")
    parser.add_argument("--postgres-dsn", default="", help="Rejected compatibility shim; use --dsn-env with cutover script instead.")
    parser.add_argument("--dsn-env", default="MAF_POSTGRES_STATE_DSN")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--operator-confirmation", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.postgres_dsn:
        return _print({"status": "failed", "error": "raw DSN CLI arguments are not allowed; use --dsn-env with MAF_POSTGRES_STATE_DSN"}, args.json, 2)
    return _print({"status": "failed", "error": "SQLite migration is disabled: PostgreSQL fresh cutover intentionally abandons SQLite history; use scripts/postgresql_state_cutover.py"}, args.json, 2)


def _print(payload: dict[str, Any], as_json: bool, code: int) -> int:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    else:
        print(payload["error"], file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

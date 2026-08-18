#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.state.runtime_factory import (  # noqa: E402
    StatePlatformBackend,
    build_state_platform_runtime_config,
)
from src.storage.mcp_dispatch_aggregate_migration import (  # noqa: E402
    MCPDispatchAggregateMigrationError,
    inspect_postgres_dispatch_aggregate,
    inspect_sqlite_dispatch_aggregate,
)


_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only MCP dispatch aggregate cutover classifier."
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Emit a closed, redacted migration classification report.",
    )
    parser.add_argument(
        "--database-path",
        help="Explicit file-backed SQLite database path.",
    )
    parser.add_argument(
        "--dsn-env",
        help="Environment variable containing the PostgreSQL operator/validation DSN.",
    )
    return parser


def run_report(args: argparse.Namespace) -> dict[str, object]:
    if not args.report:
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_report_mode_required"
        )
    if bool(args.database_path) == bool(args.dsn_env):
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_backend_options_invalid"
        )
    env = dict(os.environ)
    if args.database_path:
        env["MAF_STATE_STORE_BACKEND"] = "sqlite"
        config = build_state_platform_runtime_config(
            env=env, require_driver=False
        )
        if config.backend is not StatePlatformBackend.SQLITE_LEGACY:
            raise MCPDispatchAggregateMigrationError(
                "mcp_dispatch_aggregate_backend_mismatch"
            )
        return inspect_sqlite_dispatch_aggregate(args.database_path).as_payload()
    dsn_env = str(args.dsn_env)
    if _ENV_NAME.fullmatch(dsn_env) is None:
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_dsn_env_invalid"
        )
    dsn = (os.environ.get(dsn_env) or "").strip()
    if not dsn:
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_dsn_env_missing"
        )
    env["MAF_STATE_STORE_BACKEND"] = "postgresql"
    env["MAF_POSTGRES_STATE_DSN"] = dsn
    config = build_state_platform_runtime_config(env=env, require_driver=True)
    if config.backend is not StatePlatformBackend.POSTGRESQL or config.dsn is None:
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_backend_mismatch"
        )
    from src.storage.postgres import create_postgres_engine

    engine = create_postgres_engine(config.dsn)
    try:
        return inspect_postgres_dispatch_aggregate(engine).as_payload()
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = run_report(args)
    except MCPDispatchAggregateMigrationError as exc:
        print(
            json.dumps(
                {"result": "rejected", "reason_code": str(exc)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

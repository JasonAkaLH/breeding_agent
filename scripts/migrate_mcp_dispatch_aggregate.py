#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.state.runtime_factory import (  # noqa: E402
    StatePlatformBackend,
    build_state_platform_runtime_config,
)
from src.storage.mcp_dispatch_aggregate_migration import (  # noqa: E402
    MCPDispatchAggregateAuthorityConflictError,
    MCPDispatchAggregateMigrationError,
    apply_postgres_dispatch_aggregate,
    apply_sqlite_dispatch_aggregate,
    inspect_postgres_dispatch_aggregate,
    inspect_sqlite_dispatch_aggregate,
)


_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _load_operator_dsn(value: object) -> str:
    dsn_env = str(value)
    if _ENV_NAME.fullmatch(dsn_env) is None:
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_dsn_env_invalid"
        )
    dsn = (os.environ.get(dsn_env) or "").strip()
    if not dsn:
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_dsn_env_missing"
        )
    return dsn


@contextmanager
def _postgres_operator_engine(dsn: str, env: dict[str, str]) -> Iterator[Any]:
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
        yield engine
    finally:
        engine.dispose()


def _emit_rejected(exc: Exception, exit_code: int) -> int:
    print(
        json.dumps(
            {"result": "rejected", "reason_code": str(exc)},
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MCP dispatch aggregate cutover classifier and operator apply tool."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--report",
        action="store_true",
        help="Emit a closed, redacted migration classification report.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply an eligible report after exact report-SHA revalidation.",
    )
    parser.add_argument(
        "--expected-report-sha",
        help="Exact 64-hex SHA from the immediately preceding report.",
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
    if not getattr(args, "report", False):
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_report_mode_required"
        )
    if getattr(args, "expected_report_sha", None):
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_expected_report_sha_report_forbidden"
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
    dsn = _load_operator_dsn(args.dsn_env)
    with _postgres_operator_engine(dsn, env) as engine:
        return inspect_postgres_dispatch_aggregate(engine).as_payload()


def run_apply(args: argparse.Namespace) -> dict[str, object]:
    if not getattr(args, "apply", False):
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_apply_mode_required"
        )
    expected_report_sha = str(
        getattr(args, "expected_report_sha", None) or ""
    )
    if bool(args.database_path) == bool(args.dsn_env):
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_backend_options_invalid"
        )
    if args.database_path:
        return apply_sqlite_dispatch_aggregate(
            args.database_path,
            expected_report_sha256=expected_report_sha,
        ).as_payload()
    dsn = _load_operator_dsn(args.dsn_env)
    env = dict(os.environ)
    with _postgres_operator_engine(dsn, env) as engine:
        return apply_postgres_dispatch_aggregate(
            engine,
            expected_report_sha256=expected_report_sha,
        ).as_payload()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = run_report(args) if args.report else run_apply(args)
    except MCPDispatchAggregateAuthorityConflictError as exc:
        return _emit_rejected(exc, 3)
    except MCPDispatchAggregateMigrationError as exc:
        return _emit_rejected(exc, 2)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

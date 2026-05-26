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

from src.state.runtime_factory import StatePlatformConfigError, build_state_platform_runtime_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate PostgreSQL State Platform runtime fail-closed config.")
    parser.add_argument("--env", default="dev")
    parser.add_argument("--backend", default="sqlite")
    parser.add_argument("--dsn", default="", help="Rejected compatibility shim; use --dsn-env instead.")
    parser.add_argument("--dsn-env", default="MAF_POSTGRES_STATE_DSN")
    parser.add_argument("--allow-missing-driver", action="store_true", help="Only for non-production dry validation; production still requires driver.")
    parser.add_argument("--simulate-missing-driver", action="store_true", help="Test seam for fail-closed validation.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    env = {
        "MAF_ENV": args.env,
        "MAF_STATE_STORE_BACKEND": args.backend,
    }
    if args.dsn:
        raw_dsn_error_payload: dict[str, Any] = {
            "status": "failed",
            "error": "raw DSN CLI arguments are not allowed; use --dsn-env with MAF_POSTGRES_STATE_DSN",
        }
        if args.json:
            print(json.dumps(raw_dsn_error_payload, ensure_ascii=False, sort_keys=True))
        else:
            print(raw_dsn_error_payload["error"], file=sys.stderr)
        return 2
    if args.dsn_env and os.environ.get(args.dsn_env):
        env["MAF_POSTGRES_STATE_DSN"] = os.environ[args.dsn_env]
    normalized_env = args.env.strip().lower()
    allow_missing_driver = args.allow_missing_driver and normalized_env not in {"prod", "production"}
    try:
        config = build_state_platform_runtime_config(
            env=env,
            require_driver=not allow_missing_driver,
            driver_available=False if args.simulate_missing_driver else None,
        )
    except StatePlatformConfigError as exc:
        error_payload: dict[str, Any] = {"status": "failed", "error": str(exc)}
        if args.json:
            print(json.dumps(error_payload, ensure_ascii=False, sort_keys=True))
        else:
            print(error_payload["error"], file=sys.stderr)
        return 2
    ok_payload: dict[str, Any] = {"status": "ok", "config": config.public_dict()}
    if args.json:
        print(json.dumps(ok_payload, ensure_ascii=False, sort_keys=True, default=str))
    else:
        print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

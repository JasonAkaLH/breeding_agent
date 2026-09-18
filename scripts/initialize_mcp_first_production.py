"""One-off administrator CLI for an empty, newly introduced production MCP feature."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.integrations.mcp.rollout import MCPRolloutConfig  # noqa: E402
from src.storage.postgres.mcp_first_enablement import (  # noqa: E402
    FirstEnablementRequest,
    MCPFirstEnablementError,
    initialize_mcp_first_production,
)
from src.storage.postgres.session import create_postgres_engine  # noqa: E402


def validate_production_materials(config_path: Path) -> None:
    # These loaders do not send provider requests or construct an application runtime.
    from src.integrations.llm_client import LLMClient, bootstrap_config_env, load_config
    from src.integrations.agent_model_gate import validate_agent_model_gate
    from src.integrations.master_key import MasterKeyDeriver
    from src.api.runtime import _resolve_runtime_sidecar_artifact_trust_from_env

    if config_path.is_symlink() or not config_path.is_file():
        raise MCPFirstEnablementError("config_regular_file_required")
    bootstrap_config_env(config_path, override=True, strict=True)
    config = load_config()
    report = validate_agent_model_gate(config)
    if report.default_model_edition not in report.ready_editions:
        raise MCPFirstEnablementError("default_agent_model_unavailable")
    client = LLMClient(config=config)
    asyncio.run(client.aclose())
    MasterKeyDeriver.from_file(os.environ.get("MAF_MASTER_KEY_FILE", ""))
    metadata, _, _ = _resolve_runtime_sidecar_artifact_trust_from_env()
    if metadata is None:
        raise MCPFirstEnablementError("sidecar_trust_required")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "apply"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--backend-image-digest", required=True)
    parser.add_argument("--config", type=Path, default=Path("/app/config.yaml"))
    parser.add_argument("--expected-check-digest")
    args = parser.parse_args(argv)
    stage = "production_configuration"
    engines = []
    try:
        if args.mode == "apply" and not args.expected_check_digest:
            raise MCPFirstEnablementError("expected_check_digest_required")
        validate_production_materials(args.config)
        if (
            os.environ.get("MAF_API_ENV") or os.environ.get("MAF_ENV") or ""
        ).lower() not in {"prod", "production"}:
            raise MCPFirstEnablementError("production_environment_required")
        if os.environ.get("MCP_ROLLOUT_STAGE") != "legacy_assembly_off":
            raise MCPFirstEnablementError("final_rollout_stage_required")
        config = MCPRolloutConfig.from_env()
        request = FirstEnablementRequest(
            database_name=args.database,
            environment_id=os.environ.get("MCP_ROLLOUT_ENVIRONMENT_ID", ""),
            deployment_id=os.environ.get("MCP_ROLLOUT_DEPLOYMENT_ID", ""),
            activation_id=os.environ.get("MCP_ROLLOUT_ACTIVATION_ID", ""),
            git_sha=args.git_sha,
            backend_image_digest=args.backend_image_digest,
        )
        request.validate()
        stage = "database_preconditions"
        for name in ("MAF_MCP_INITIALIZATION_ADMIN_DSN", "MAF_MCP_ROLLOUT_APP_DSN"):
            dsn = os.environ.get(name, "")
            if not dsn or dsn != dsn.strip():
                raise MCPFirstEnablementError("dedicated_database_credentials_required")
            engines.append(create_postgres_engine(dsn))
        result = initialize_mcp_first_production(
            *engines,
            request,
            config,
            apply=args.mode == "apply",
            expected_check_digest=args.expected_check_digest,
        )
        print(
            json.dumps(
                {"first_production_enablement": "passed", **result}, sort_keys=True
            )
        )
        return 0
    except Exception as error:
        code = (
            str(error)
            if isinstance(error, MCPFirstEnablementError)
            else "precondition_or_transaction_failed"
        )
        print(
            json.dumps(
                {"first_production_enablement": "failed", "stage": stage, "code": code}
            )
        )
        return 1
    finally:
        for engine in engines:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

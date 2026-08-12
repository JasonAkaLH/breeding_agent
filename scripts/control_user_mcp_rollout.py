#!/usr/bin/env python
"""Operate the append-only user-MCP phase-3 rollout ledger."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_user_mcp_phase3_evidence import (  # noqa: E402
    ATTESTATION_KEYRING_ENV,
    Phase3EvidenceError,
    evaluate_artifact,
    load_attestation_keyring,
    load_artifact,
)
from src.core.models import (  # noqa: E402
    MCPRolloutBlockResolution,
    MCPRolloutDeploymentActivation,
    MCPRolloutEvidenceSnapshot,
    MCPRolloutPromotionBlock,
    MCPRolloutStageApproval,
)
from src.integrations.mcp.rollout import (  # noqa: E402
    MCPRolloutConfig,
    MCPRoutingMode,
)
from src.integrations.mcp.observability import (  # noqa: E402
    mcp_evidence_snapshot_matches_record,
    validate_mcp_evidence_snapshot_record,
)
from src.integrations.mcp.rollout_evidence import (  # noqa: E402
    MCPEvidenceSnapshot,
    MCPEvidenceSource,
    MCPGateBlocker,
    MCPRolloutStage,
    is_provable_mcp_exposure_decrease,
    validate_evidence_snapshot,
)
from src.state.runtime_factory import (  # noqa: E402
    StatePlatformBackend,
    build_state_platform_runtime_config,
)
from src.storage.postgres import (  # noqa: E402
    PostgreSQLStorage,
    create_postgres_engine,
    create_postgres_session_factory,
    validate_mcp_rollout_connection_role,
)
from src.storage.sqlite import (  # noqa: E402
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)


class RolloutControlError(ValueError):
    """Operator-safe, fail-closed rollout control error."""


class RolloutStorage(Protocol):
    async def get_mcp_rollout_evidence_snapshot(
        self, evidence_id: str
    ) -> MCPRolloutEvidenceSnapshot | None: ...

    async def append_mcp_rollout_stage_approval(
        self, approval: MCPRolloutStageApproval
    ) -> MCPRolloutStageApproval: ...

    async def activate_mcp_rollout_deployment(
        self, activation: MCPRolloutDeploymentActivation
    ) -> MCPRolloutDeploymentActivation: ...

    async def append_mcp_rollout_promotion_block(
        self, block: MCPRolloutPromotionBlock
    ) -> MCPRolloutPromotionBlock: ...

    async def append_mcp_rollout_block_resolution(
        self, resolution: MCPRolloutBlockResolution
    ) -> MCPRolloutBlockResolution: ...


async def append_approval(
    storage: RolloutStorage,
    *,
    approval_id: str,
    target_deployment_id: str,
    candidate_config: MCPRolloutConfig,
    target_stage: MCPRolloutStage,
    evidence: MCPEvidenceSnapshot,
    reason: str,
    approver: str,
    created_at: datetime,
    trusted_attestation_keys: Mapping[str, bytes] | None = None,
) -> MCPRolloutStageApproval:
    _required(reason=reason, approver=approver, evidence_id=evidence.evidence_id)
    _validate_config_stage(candidate_config, target_stage)
    _require_transition_source(evidence, target_stage, trusted_attestation_keys)
    await _require_stored_evidence(storage, evidence, trusted_attestation_keys)
    return await storage.append_mcp_rollout_stage_approval(
        MCPRolloutStageApproval(
            approval_id=approval_id,
            environment_id=evidence.environment_id,
            deployment_id=target_deployment_id,
            stage=target_stage.value,
            config_fingerprint=candidate_config.fingerprint,
            evidence_id=evidence.evidence_id,
            reason=reason,
            approver=approver,
            created_at=created_at,
        )
    )


async def append_block(
    storage: RolloutStorage,
    *,
    block_id: str,
    evidence: MCPEvidenceSnapshot,
    reason_code: MCPGateBlocker,
    reason: str,
    approver: str,
    created_at: datetime,
    trusted_attestation_keys: Mapping[str, bytes] | None = None,
) -> MCPRolloutPromotionBlock:
    _required(reason=reason, approver=approver, evidence_id=evidence.evidence_id)
    await _require_stored_evidence(storage, evidence, trusted_attestation_keys)
    return await storage.append_mcp_rollout_promotion_block(
        MCPRolloutPromotionBlock(
            block_id=block_id,
            environment_id=evidence.environment_id,
            deployment_id=evidence.deployment_id,
            stage=evidence.stage.value,
            config_fingerprint=evidence.config_fingerprint,
            evidence_id=evidence.evidence_id,
            reason_code=reason_code.value,
            created_at=created_at,
        )
    )


async def resolve_block(
    storage: RolloutStorage,
    *,
    resolution_id: str,
    block_id: str,
    approval_id: str,
    evidence: MCPEvidenceSnapshot,
    reason: str,
    approver: str,
    created_at: datetime,
    trusted_attestation_keys: Mapping[str, bytes] | None = None,
) -> MCPRolloutBlockResolution:
    _required(reason=reason, approver=approver, evidence_id=evidence.evidence_id)
    _require_production_evidence(evidence, trusted_attestation_keys)
    await _require_stored_evidence(storage, evidence, trusted_attestation_keys)
    return await storage.append_mcp_rollout_block_resolution(
        MCPRolloutBlockResolution(
            resolution_id=resolution_id,
            block_id=block_id,
            approval_id=approval_id,
            evidence_id=evidence.evidence_id,
            reason=reason,
            approver=approver,
            created_at=created_at,
        )
    )


async def activate_approval(
    storage: RolloutStorage,
    *,
    activation_id: str,
    approval_id: str,
    target_deployment_id: str,
    candidate_config: MCPRolloutConfig,
    target_stage: MCPRolloutStage,
    evidence: MCPEvidenceSnapshot,
    previous_activation_id: str | None,
    reason: str,
    approver: str,
    created_at: datetime,
    trusted_attestation_keys: Mapping[str, bytes] | None = None,
) -> MCPRolloutDeploymentActivation:
    _required(reason=reason, approver=approver, evidence_id=evidence.evidence_id)
    _validate_config_stage(candidate_config, target_stage)
    _require_transition_source(evidence, target_stage, trusted_attestation_keys)
    await _require_stored_evidence(storage, evidence, trusted_attestation_keys)
    return await storage.activate_mcp_rollout_deployment(
        MCPRolloutDeploymentActivation(
            activation_id=activation_id,
            environment_id=evidence.environment_id,
            deployment_id=target_deployment_id,
            stage=target_stage.value,
            config_fingerprint=candidate_config.fingerprint,
            approval_id=approval_id,
            evidence_id=evidence.evidence_id,
            previous_activation_id=previous_activation_id,
            operator_reason=reason,
            is_rollback=False,
            created_at=created_at,
        )
    )


async def rollback_activation(
    storage: RolloutStorage,
    *,
    activation_id: str,
    approval_id: str,
    target_deployment_id: str,
    current_config: MCPRolloutConfig,
    candidate_config: MCPRolloutConfig,
    target_stage: MCPRolloutStage,
    evidence: MCPEvidenceSnapshot,
    previous_activation_id: str,
    reason: str,
    approver: str,
    created_at: datetime,
    trusted_attestation_keys: Mapping[str, bytes] | None = None,
) -> MCPRolloutDeploymentActivation:
    _required(reason=reason, approver=approver, evidence_id=evidence.evidence_id)
    _validate_config_stage(candidate_config, target_stage)
    _validate_config_stage(current_config, evidence.stage)
    if current_config.fingerprint != evidence.config_fingerprint:
        raise RolloutControlError(
            "current config fingerprint does not match rollback evidence"
        )
    if not is_provable_mcp_exposure_decrease(current_config, candidate_config):
        raise RolloutControlError(
            "rollback candidate is not a strict MCP exposure decrease"
        )
    _require_production_evidence(evidence, trusted_attestation_keys)
    await _require_stored_evidence(storage, evidence, trusted_attestation_keys)
    return await storage.activate_mcp_rollout_deployment(
        MCPRolloutDeploymentActivation(
            activation_id=activation_id,
            environment_id=evidence.environment_id,
            deployment_id=target_deployment_id,
            stage=target_stage.value,
            config_fingerprint=candidate_config.fingerprint,
            approval_id=approval_id,
            evidence_id=evidence.evidence_id,
            previous_activation_id=previous_activation_id,
            operator_reason=reason,
            is_rollback=True,
            created_at=created_at,
        )
    )


async def execute(args: argparse.Namespace, storage: RolloutStorage) -> Any:
    artifact = load_artifact(Path(args.evidence))
    trusted_attestation_keys = (
        None
        if args.attestation_keyring is None
        else load_attestation_keyring(Path(args.attestation_keyring))
    )
    request, records, evaluation = evaluate_artifact(
        artifact,
        trusted_attestation_keys=trusted_attestation_keys,
    )
    if not evaluation.allowed and args.command != "append-block":
        blockers = ",".join(item.value for item in evaluation.blockers)
        raise RolloutControlError(f"evidence gate blocked: {blockers}")
    evidence = next(item for item in records if item.evidence_id == request.evidence_id)
    now = _timestamp(args.created_at)
    if args.command == "append-approval":
        _assert_request(args, request)
        return await append_approval(
            storage,
            approval_id=args.approval_id,
            target_deployment_id=args.deployment_id,
            candidate_config=_load_config(Path(args.candidate_config)),
            target_stage=MCPRolloutStage(args.target_stage),
            evidence=evidence,
            reason=args.reason,
            approver=args.approver,
            created_at=now,
            trusted_attestation_keys=trusted_attestation_keys,
        )
    if args.command == "append-block":
        reason_code = MCPGateBlocker(args.reason_code)
        if reason_code not in evaluation.blockers:
            raise RolloutControlError(
                "block reason is not present in the evidence evaluation"
            )
        return await append_block(
            storage,
            block_id=args.block_id,
            evidence=evidence,
            reason_code=reason_code,
            reason=args.reason,
            approver=args.approver,
            created_at=now,
            trusted_attestation_keys=trusted_attestation_keys,
        )
    if args.command == "resolve-block":
        return await resolve_block(
            storage,
            resolution_id=args.resolution_id,
            block_id=args.block_id,
            approval_id=args.approval_id,
            evidence=evidence,
            reason=args.reason,
            approver=args.approver,
            created_at=now,
            trusted_attestation_keys=trusted_attestation_keys,
        )
    if args.command == "activate":
        _assert_request(args, request)
        return await activate_approval(
            storage,
            activation_id=args.activation_id,
            approval_id=args.approval_id,
            target_deployment_id=args.deployment_id,
            candidate_config=_load_config(Path(args.candidate_config)),
            target_stage=MCPRolloutStage(args.target_stage),
            evidence=evidence,
            previous_activation_id=args.previous_activation_id,
            reason=args.reason,
            approver=args.approver,
            created_at=now,
            trusted_attestation_keys=trusted_attestation_keys,
        )
    if args.command == "rollback":
        return await rollback_activation(
            storage,
            activation_id=args.activation_id,
            approval_id=args.approval_id,
            target_deployment_id=args.deployment_id,
            current_config=_load_config(Path(args.current_config)),
            candidate_config=_load_config(Path(args.candidate_config)),
            target_stage=MCPRolloutStage(args.target_stage),
            evidence=evidence,
            previous_activation_id=args.previous_activation_id,
            reason=args.reason,
            approver=args.approver,
            created_at=now,
            trusted_attestation_keys=trusted_attestation_keys,
        )
    raise RolloutControlError("unsupported command")


def run(
    argv: list[str] | None = None,
    *,
    storage: RolloutStorage | None = None,
    stdout: Any = sys.stdout,
    stderr: Any = sys.stderr,
) -> int:
    args = _parser().parse_args(argv)
    engine = None
    try:
        if storage is None:
            storage, engine = _configured_storage(args)
        result = asyncio.run(execute(args, storage))
        print(
            json.dumps(
                {"status": "ok", "record": _json_value(asdict(result))}, sort_keys=True
            ),
            file=stdout,
        )
        return 0
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        Phase3EvidenceError,
        RolloutControlError,
        TypeError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"status": "failed", "error": str(exc) or type(exc).__name__},
                sort_keys=True,
            ),
            file=stderr,
        )
        return 2
    finally:
        if engine is not None:
            engine.dispose()


async def _require_stored_evidence(
    storage: RolloutStorage,
    evidence: MCPEvidenceSnapshot,
    trusted_attestation_keys: Mapping[str, bytes] | None,
) -> MCPRolloutEvidenceSnapshot:
    stored = await storage.get_mcp_rollout_evidence_snapshot(evidence.evidence_id)
    if stored is None:
        raise RolloutControlError("evidence must already exist in canonical storage")
    blockers = validate_mcp_evidence_snapshot_record(
        stored,
        trusted_attestation_keys=trusted_attestation_keys,
    )
    if blockers:
        rendered = ",".join(item.value for item in blockers)
        raise RolloutControlError(f"canonical stored evidence is invalid: {rendered}")
    if not mcp_evidence_snapshot_matches_record(evidence, stored):
        raise RolloutControlError(
            "caller evidence does not match canonical stored evidence"
        )
    return stored


def _require_transition_source(
    evidence: MCPEvidenceSnapshot,
    target_stage: MCPRolloutStage,
    trusted_attestation_keys: Mapping[str, bytes] | None,
) -> None:
    _require_valid_attestation(evidence, trusted_attestation_keys)
    if (
        target_stage is MCPRolloutStage.INTERNAL_SHADOW
        and evidence.stage is MCPRolloutStage.OFF
    ):
        if evidence.source is not MCPEvidenceSource.CI:
            raise RolloutControlError("off-to-shadow activation requires CI evidence")
        return
    _require_production_evidence(evidence, trusted_attestation_keys)


def _require_production_evidence(
    evidence: MCPEvidenceSnapshot,
    trusted_attestation_keys: Mapping[str, bytes] | None,
) -> None:
    if evidence.source is not MCPEvidenceSource.PRODUCTION:
        raise RolloutControlError(
            "CI evidence cannot authorize a production rollout transition"
        )
    _require_valid_attestation(evidence, trusted_attestation_keys)


def _require_valid_attestation(
    evidence: MCPEvidenceSnapshot,
    trusted_attestation_keys: Mapping[str, bytes] | None,
) -> None:
    blockers = validate_evidence_snapshot(
        evidence,
        trusted_attestation_keys=trusted_attestation_keys,
    )
    attestation_blockers = tuple(
        item
        for item in blockers
        if item
        in {
            MCPGateBlocker.ATTESTATION_MISSING,
            MCPGateBlocker.ATTESTATION_INVALID,
        }
    )
    if attestation_blockers:
        rendered = ",".join(item.value for item in attestation_blockers)
        raise RolloutControlError(f"evidence snapshot is invalid: {rendered}")


def _required(*, reason: str, approver: str, evidence_id: str) -> None:
    if not reason.strip():
        raise RolloutControlError("reason is required")
    if not approver.strip():
        raise RolloutControlError("approver is required")
    if not evidence_id.strip():
        raise RolloutControlError("evidence is required")


def _validate_config_stage(config: MCPRolloutConfig, stage: MCPRolloutStage) -> None:
    valid = False
    if stage is MCPRolloutStage.OFF:
        valid = config.routing_mode is MCPRoutingMode.OFF
    elif stage is MCPRolloutStage.INTERNAL_SHADOW:
        valid = config.routing_mode is MCPRoutingMode.SHADOW
    elif stage in {MCPRolloutStage.INTERNAL_ENFORCE, MCPRolloutStage.COHORT_ENFORCE}:
        valid = config.routing_mode is MCPRoutingMode.ENFORCE and config.legacy_enabled
    elif stage is MCPRolloutStage.FULL_ENFORCE:
        valid = (
            config.routing_mode is MCPRoutingMode.ENFORCE
            and config.legacy_enabled
            and config.enforce_percent == 100
            and not config.enforce_cohorts
        )
    elif stage is MCPRolloutStage.LEGACY_ASSEMBLY_OFF:
        valid = (
            config.routing_mode is MCPRoutingMode.ENFORCE and not config.legacy_enabled
        )
    if not valid:
        raise RolloutControlError(
            "candidate config does not match target rollout stage"
        )


def _assert_request(args: argparse.Namespace, request: Any) -> None:
    if MCPRolloutStage(args.target_stage) is not request.target_stage:
        raise RolloutControlError("target stage does not match evidence gate request")


def _load_config(path: Path) -> MCPRolloutConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise RolloutControlError("rollout config file must be a JSON object")
    env = raw.get("env", raw)
    if not isinstance(env, Mapping) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()
    ):
        raise RolloutControlError("rollout config env must contain string values")
    return MCPRolloutConfig.from_env(env)


def _configured_storage(args: argparse.Namespace) -> tuple[RolloutStorage, Any]:
    config = build_state_platform_runtime_config(env=os.environ, require_driver=True)
    if config.backend is StatePlatformBackend.SQLITE_LEGACY:
        deployment_env = (
            os.environ.get("MAF_API_ENV") or os.environ.get("MAF_ENV") or "dev"
        ).lower()
        if deployment_env not in {
            "dev",
            "development",
            "local",
            "test",
            "testing",
            "ci",
        }:
            raise RolloutControlError(
                "SQLite rollout control is limited to local and CI environments"
            )
        engine = create_sqlite_engine(Path(args.database_path))
        bootstrap_sqlite_database(engine)
        return SQLiteStorage(create_sqlite_session_factory(engine)), engine
    if args.command == "append-block":
        role = "evaluator"
        dsn_env = "MAF_MCP_ROLLOUT_EVALUATOR_DSN"
    else:
        role = "operator"
        dsn_env = "MAF_MCP_ROLLOUT_OPERATOR_DSN"
    dsn = (os.environ.get(dsn_env) or "").strip()
    if not dsn:
        raise RolloutControlError(f"production PostgreSQL {role} role DSN is unavailable")
    engine = None
    try:
        engine = create_postgres_engine(dsn)
        validate_mcp_rollout_connection_role(engine, role)
    except Exception as exc:
        if engine is not None:
            engine.dispose()
        raise RolloutControlError(
            f"production PostgreSQL {role} role is invalid or unavailable"
        ) from exc
    factory = create_postgres_session_factory(engine)
    return PostgreSQLStorage(
        factory,
        mcp_rollout_session_factory=factory,
        mcp_rollout_role=role,
    ), engine


def _timestamp(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RolloutControlError("created-at must include a timezone")
    return parsed


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Control the user-MCP phase-3 rollout ledger."
    )
    parser.add_argument(
        "--database-path",
        default=os.environ.get("MAF_SQLITE_DEV_PATH", "runtime/dev.sqlite3"),
    )
    parser.add_argument(
        "--attestation-keyring",
        default=os.environ.get(ATTESTATION_KEYRING_ENV),
        help=(
            "Trusted production-attestation keyring path; defaults to "
            f"${ATTESTATION_KEYRING_ENV}."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    approval = subparsers.add_parser("append-approval")
    _common_evidence(approval)
    _target(approval)
    _audit(approval)
    approval.add_argument("--approval-id", required=True)

    block = subparsers.add_parser("append-block")
    _common_evidence(block)
    _audit(block)
    block.add_argument("--block-id", required=True)
    block.add_argument(
        "--reason-code", required=True, choices=[item.value for item in MCPGateBlocker]
    )

    resolution = subparsers.add_parser("resolve-block")
    _common_evidence(resolution)
    _audit(resolution)
    resolution.add_argument("--resolution-id", required=True)
    resolution.add_argument("--block-id", required=True)
    resolution.add_argument("--approval-id", required=True)

    activation = subparsers.add_parser("activate")
    _common_evidence(activation)
    _target(activation)
    _audit(activation)
    activation.add_argument("--activation-id", required=True)
    activation.add_argument("--approval-id", required=True)
    activation.add_argument("--previous-activation-id")

    rollback = subparsers.add_parser("rollback")
    _common_evidence(rollback)
    _target(rollback)
    _audit(rollback)
    rollback.add_argument("--activation-id", required=True)
    rollback.add_argument("--approval-id", required=True)
    rollback.add_argument("--previous-activation-id", required=True)
    rollback.add_argument("--current-config", required=True)
    return parser


def _common_evidence(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--evidence",
        required=True,
        help="Validated canonical evidence artifact; never imported into storage.",
    )
    parser.add_argument("--created-at")


def _target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument(
        "--target-stage",
        required=True,
        choices=[item.value for item in MCPRolloutStage],
    )
    parser.add_argument("--candidate-config", required=True)


def _audit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--reason", required=True)
    parser.add_argument("--approver", required=True)


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())

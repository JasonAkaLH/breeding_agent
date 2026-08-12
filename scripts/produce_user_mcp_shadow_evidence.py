#!/usr/bin/env python
"""Produce signed internal-shadow evidence from durable storage only."""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.integrations.mcp.observability import mcp_evidence_snapshot_to_record  # noqa: E402
from src.integrations.mcp.rollout_evidence import (  # noqa: E402
    MCPEvidenceProducer,
    MCPEvidenceSnapshot,
    MCPEvidenceSource,
    MCPRedLineCount,
    MCPRolloutStage,
    MCPSafetyRedLine,
)
from src.integrations.mcp.shadow_evidence import (  # noqa: E402
    build_internal_shadow_evidence_payload,
    metric_records_to_domain,
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


ATTESTATION_KEY_ENV = "MAF_MCP_ROLLOUT_ATTESTATION_KEY_B64"


async def produce(args: argparse.Namespace):
    storage, engine = _storage(args.database_path)
    key = _attestation_key()
    started_at = _timestamp(args.window_started_at)
    ended_at = _timestamp(args.window_ended_at)
    recorded_at = datetime.now(timezone.utc)

    if isinstance(storage, PostgreSQLStorage):
        try:
            return await storage.produce_mcp_rollout_evidence_snapshot_db_derived(
                args.environment_id,
                args.deployment_id,
                git_sha=args.git_sha,
                window_started_at=started_at,
                window_ended_at=ended_at,
                attestation_key_id=args.attestation_key_id,
                attestation_key=key,
            )
        finally:
            engine.dispose()

    for name in (
        "config_fingerprint",
        "manifest_fingerprint",
        "fixture_fingerprint",
        "mapping_fingerprint",
        "evidence_id",
        "nonce",
        "snapshot_id",
    ):
        if getattr(args, name, None) in {None, ""}:
            engine.dispose()
            raise ValueError(f"--{name.replace('_', '-')} is required for SQLite")

    def builder(samples, metric_records):
        metrics = metric_records_to_domain(metric_records)
        red_lines = tuple(
            MCPRedLineCount(
                red_line=red_line,
                count=sum(
                    bucket.value
                    for bucket in metrics
                    if bucket.labels.red_line is red_line
                ),
            )
            for red_line in MCPSafetyRedLine
        )
        payload = build_internal_shadow_evidence_payload(
            samples,
            environment_id=args.environment_id,
            deployment_id=args.deployment_id,
            config_fingerprint=args.config_fingerprint,
            manifest_fingerprint=args.manifest_fingerprint,
            fixture_fingerprint=args.fixture_fingerprint,
            mapping_fingerprint=args.mapping_fingerprint,
            window_started_at=started_at,
            window_ended_at=ended_at,
            metric_buckets=metrics,
        )
        payload = replace(payload, red_line_counts=red_lines)
        snapshot = MCPEvidenceSnapshot.seal(
            evidence_id=args.evidence_id,
            environment_id=args.environment_id,
            git_sha=args.git_sha,
            deployment_id=args.deployment_id,
            stage=MCPRolloutStage.INTERNAL_SHADOW,
            config_fingerprint=args.config_fingerprint,
            window_started_at=started_at,
            window_ended_at=ended_at,
            recorded_at=recorded_at,
            producer=MCPEvidenceProducer.PRODUCTION_SNAPSHOT,
            source=MCPEvidenceSource.PRODUCTION,
            snapshot_id=args.snapshot_id,
            nonce=args.nonce,
            payload=payload,
            attestation_key_id=args.attestation_key_id,
            attestation_key=key,
        )
        return mcp_evidence_snapshot_to_record(
            snapshot,
            trusted_attestation_keys={args.attestation_key_id: key},
        )

    try:
        return await storage.produce_mcp_shadow_evidence_snapshot(
            args.environment_id,
            args.deployment_id,
            window_started_at=started_at,
            window_ended_at=ended_at,
            builder=builder,
        )
    finally:
        engine.dispose()


def _storage(database_path: str):
    config = build_state_platform_runtime_config(env=os.environ, require_driver=True)
    if config.backend is StatePlatformBackend.SQLITE_LEGACY:
        deployment_env = (os.environ.get("MAF_API_ENV") or "dev").lower()
        if deployment_env not in {"dev", "development", "local", "test", "testing", "ci"}:
            raise ValueError("SQLite shadow evidence production is limited to local and CI")
        engine = create_sqlite_engine(Path(database_path))
        bootstrap_sqlite_database(engine)
        return SQLiteStorage(create_sqlite_session_factory(engine)), engine
    dsn = (os.environ.get("MAF_MCP_ROLLOUT_SNAPSHOT_DSN") or "").strip()
    if not dsn:
        raise ValueError("production PostgreSQL snapshot role DSN is unavailable")
    engine = None
    try:
        engine = create_postgres_engine(dsn)
        validate_mcp_rollout_connection_role(engine, "snapshot")
    except Exception as exc:
        if engine is not None:
            engine.dispose()
        raise ValueError(
            "production PostgreSQL snapshot role is invalid or unavailable"
        ) from exc
    factory = create_postgres_session_factory(engine)
    return PostgreSQLStorage(
        factory,
        mcp_rollout_session_factory=factory,
        mcp_rollout_role="snapshot",
    ), engine


def _attestation_key() -> bytes:
    raw = os.environ.get(ATTESTATION_KEY_ENV, "")
    try:
        key = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise ValueError(f"{ATTESTATION_KEY_ENV} must be canonical base64") from exc
    if not key or base64.b64encode(key).decode("ascii") != raw:
        raise ValueError(f"{ATTESTATION_KEY_ENV} must be canonical base64")
    return key


def _timestamp(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("evidence timestamps must include a timezone")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Produce internal-shadow evidence from durable samples and metrics."
    )
    parser.add_argument("--database-path", default="runtime/dev.sqlite3")
    for name in (
        "environment-id", "deployment-id", "git-sha", "window-started-at",
        "window-ended-at", "attestation-key-id",
    ):
        parser.add_argument(f"--{name}", required=True)
    for name in (
        "config-fingerprint", "manifest-fingerprint", "fixture-fingerprint",
        "mapping-fingerprint", "evidence-id", "nonce",
    ):
        parser.add_argument(
            f"--{name}",
            help="Local/CI SQLite compatibility only; PostgreSQL derives this value.",
        )
    parser.add_argument("--snapshot-id", type=int)
    return parser


def main() -> int:
    record = asyncio.run(produce(_parser().parse_args()))
    print(record.evidence_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

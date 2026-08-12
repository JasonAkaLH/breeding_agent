#!/usr/bin/env python
"""Build, validate, and safely apply a legacy MCP migration artifact.

Apply is explicit and fail-closed. The built-in backend writes only canonical
user-scoped MCP config and encrypted credentials to the configured State
Platform backend; runtime state is never migrated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.integrations.mcp.config import (  # noqa: E402
    MCPRuntimeConfig,
    load_mcp_server_config,
)
from src.integrations.mcp.credentials import CredentialSecurityError  # noqa: E402
from src.integrations.mcp.legacy_migration import (  # noqa: E402
    LEGACY_MIGRATION_HEALTH_POLICY,
    LegacyConsumerScope,
    LegacyDisposition,
    LegacyMigrationHealthPolicy,
    LegacyMigrationHealthResult,
    LegacyMigrationPlan,
    LegacyMigrationValidationError,
    LegacyServerClassification,
    legacy_migration_record_id,
    plan_legacy_mcp_config_migration,
    validate_legacy_migration_apply,
)
from src.integrations.mcp.legacy_migration_apply import (  # noqa: E402
    BuiltInLegacyMigrationLiveHealthValidator,
    LegacyMigrationApplyError,
    LegacyMigrationLiveHealthValidator,
    LocalLegacyMigrationApplier,
)
from src.state.runtime_factory import (  # noqa: E402
    StatePlatformBackend,
    StatePlatformConfigError,
    build_state_platform_runtime_config,
)


ARTIFACT_SCHEMA = "maf.legacy_mcp_migration.v1"


class MigrationCommandError(RuntimeError):
    """An operator-safe, fail-closed command error."""


class HealthValidator(Protocol):
    def __call__(
        self,
        config: MCPRuntimeConfig,
        plan: LegacyMigrationPlan,
        policy: LegacyMigrationHealthPolicy,
    ) -> Iterable[LegacyMigrationHealthResult]: ...


class ArtifactApplier(Protocol):
    def __call__(self, artifact: Mapping[str, Any], *, idempotency_key: str) -> str: ...


def build_artifact(
    *,
    config: MCPRuntimeConfig,
    classifications: Iterable[LegacyServerClassification],
    health_validator: HealthValidator | None = None,
) -> dict[str, Any]:
    decisions = tuple(classifications)
    plan = plan_legacy_mcp_config_migration(config, decisions)
    results = tuple(
        (health_validator or _health_unavailable)(
            config, plan, LEGACY_MIGRATION_HEALTH_POLICY
        )
    )
    validation = validate_legacy_migration_apply(
        plan,
        results,
        require_continuity_attestation=False,
    )
    payload: dict[str, Any] = {
        "schema": ARTIFACT_SCHEMA,
        "plan": _plan_payload(plan),
        "health_policy": asdict(LEGACY_MIGRATION_HEALTH_POLICY),
        "health_results": [
            asdict(item) for item in sorted(results, key=lambda item: item.server_id)
        ],
        "health_evidence_role": "informational_only_not_security_attestation",
        "apply_validation": {
            "ready": validation.ready,
            "blockers": list(validation.blockers),
        },
        "retirement_evidence": _retirement_evidence(decisions, plan),
    }
    payload["artifact_fingerprint"] = _fingerprint(payload)
    return payload


def validate_artifact_for_apply(
    artifact: Mapping[str, Any],
    *,
    current_config: MCPRuntimeConfig,
    current_classifications: Iterable[LegacyServerClassification],
) -> LegacyMigrationPlan:
    raw = dict(artifact)
    fingerprint = raw.pop("artifact_fingerprint", None)
    if not isinstance(fingerprint, str) or fingerprint != _fingerprint(raw):
        raise MigrationCommandError("migration_artifact_tampered")
    if raw.get("schema") != ARTIFACT_SCHEMA:
        raise MigrationCommandError("migration_artifact_schema_invalid")

    decisions = tuple(current_classifications)
    plan = plan_legacy_mcp_config_migration(current_config, decisions)
    stored_plan = raw.get("plan")
    if (
        not isinstance(stored_plan, Mapping)
        or stored_plan.get("plan_fingerprint") != plan.plan_fingerprint
    ):
        raise MigrationCommandError("migration_artifact_stale")
    if dict(stored_plan) != _plan_payload(plan):
        raise MigrationCommandError("migration_artifact_plan_mismatch")
    if raw.get("health_policy") != asdict(LEGACY_MIGRATION_HEALTH_POLICY):
        raise MigrationCommandError("migration_artifact_health_policy_mismatch")
    if raw.get("health_evidence_role") != "informational_only_not_security_attestation":
        raise MigrationCommandError("migration_artifact_health_role_invalid")
    if plan.retained_server_ids:
        raise MigrationCommandError("retained_legacy_servers_block_apply")
    if not plan.assembly_off_allowed:
        raise MigrationCommandError("assembly_off_classification_blocked")

    results = _health_results(raw.get("health_results"))
    validation = validate_legacy_migration_apply(
        plan,
        results,
        require_continuity_attestation=False,
    )
    if raw.get("apply_validation") != {
        "ready": validation.ready,
        "blockers": list(validation.blockers),
    }:
        raise MigrationCommandError("migration_artifact_apply_validation_mismatch")
    retirement_evidence = raw.get("retirement_evidence")
    _validate_retirement_evidence(retirement_evidence, plan)
    if retirement_evidence != _retirement_evidence(decisions, plan):
        raise MigrationCommandError("retirement_evidence_stale")
    return plan


def run(
    argv: list[str] | None = None,
    *,
    health_validator: HealthValidator | None = None,
    artifact_applier: ArtifactApplier | None = None,
    live_health_validator: LegacyMigrationLiveHealthValidator | None = None,
    live_validator_provenance: str | None = None,
    stdout: Any = sys.stdout,
    stderr: Any = sys.stderr,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = load_mcp_server_config(path=args.config)
        classifications = load_classifications(Path(args.classifications))
        if not args.apply:
            artifact = build_artifact(
                config=config,
                classifications=classifications,
                health_validator=health_validator,
            )
            if args.artifact_out:
                _write_artifact(Path(args.artifact_out), artifact)
            print(json.dumps(artifact, sort_keys=True), file=stdout)
            return 0

        if not args.artifact:
            raise MigrationCommandError("apply_requires_artifact")
        artifact = _load_json_object(Path(args.artifact), "migration_artifact_invalid")
        plan = validate_artifact_for_apply(
            artifact,
            current_config=config,
            current_classifications=classifications,
        )
        selected_applier = artifact_applier
        built_in_applier = selected_applier is None
        if built_in_applier:
            selected_applier = _build_local_applier(
                args,
                config=config,
                plan=plan,
                live_health_validator=live_health_validator,
                live_validator_provenance=live_validator_provenance,
            )
        assert selected_applier is not None
        outcome = selected_applier(artifact, idempotency_key=plan.plan_fingerprint)
        if outcome not in {"applied", "already_applied"}:
            raise MigrationCommandError("apply_backend_result_invalid")
        result = {
            "status": outcome,
            "artifact_fingerprint": artifact["artifact_fingerprint"],
            "plan_fingerprint": plan.plan_fingerprint,
        }
        if built_in_applier:
            result["durable_record_ids"] = [
                legacy_migration_record_id(
                    plan_fingerprint=plan.plan_fingerprint,
                    source_server_id=candidate.source_server_id,
                    target_server_id=candidate.target_server_id,
                )
                for candidate in plan.mapping_candidates
            ]
        if built_in_applier and args.audit_out:
            try:
                _write_audit(Path(args.audit_out), result, plan)
            except MigrationCommandError:
                result["audit_export"] = "failed_non_authoritative"
        print(json.dumps(result, sort_keys=True), file=stdout)
        return 0
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        LegacyMigrationValidationError,
        LegacyMigrationApplyError,
        CredentialSecurityError,
        MigrationCommandError,
        StatePlatformConfigError,
        TypeError,
        ValueError,
    ) as exc:
        message = str(exc) or type(exc).__name__
        print(
            json.dumps({"status": "failed", "error": message}, sort_keys=True),
            file=stderr,
        )
        return 2


def load_classifications(path: Path) -> tuple[LegacyServerClassification, ...]:
    payload = _load_json_object(path, "legacy_classifications_invalid")
    raw_servers = payload.get("servers")
    if not isinstance(raw_servers, Mapping):
        raise MigrationCommandError("legacy_classifications_requires_servers_object")
    decisions: list[LegacyServerClassification] = []
    for server_id, value in raw_servers.items():
        if not isinstance(value, Mapping):
            raise MigrationCommandError(f"legacy_classification_invalid:{server_id}")
        allowed = {
            "disposition",
            "consumer_scope",
            "owner_user_id",
            "retirement_approver",
            "retirement_reason",
            "impact_accepted",
            "target_consumer_refs",
        }
        if set(value) - allowed:
            raise MigrationCommandError(
                f"legacy_classification_fields_invalid:{server_id}"
            )
        try:
            decisions.append(
                LegacyServerClassification(
                    server_id=str(server_id),
                    disposition=LegacyDisposition(value.get("disposition")),
                    consumer_scope=LegacyConsumerScope(value.get("consumer_scope")),
                    owner_user_id=_optional_string(value.get("owner_user_id")),
                    retirement_approver=_optional_string(
                        value.get("retirement_approver")
                    ),
                    retirement_reason=_optional_string(value.get("retirement_reason")),
                    impact_accepted=value.get("impact_accepted") is True,
                    target_consumer_refs=_safe_consumer_refs(
                        value.get("target_consumer_refs"), server_id=str(server_id)
                    ),
                )
            )
        except ValueError as exc:
            raise MigrationCommandError(
                f"legacy_classification_enum_invalid:{server_id}"
            ) from exc
    return tuple(decisions)


def _plan_payload(plan: LegacyMigrationPlan) -> dict[str, Any]:
    return {
        "inventory": [asdict(item) for item in plan.inventory],
        "mapping_candidates": [asdict(item) for item in plan.mapping_candidates],
        "consumer_capability_impact": [
            {
                "server_id": item.server_id,
                "consumer_scope": item.consumer_scope.value,
                "disposition": item.disposition.value,
                "configured_tool_count": item.configured_tool_count,
                "exposed_capability_ids": list(item.exposed_capability_ids),
                "target_consumer_count": item.target_consumer_count,
                "target_consumer_set_digest": item.target_consumer_set_digest,
                "obligations": [
                    {
                        "consumer_ref": obligation.consumer_ref,
                        "capability_id": obligation.capability_id,
                        "source_contract_fingerprint": (
                            obligation.source_contract_fingerprint
                        ),
                        "resolution": obligation.resolution.value,
                    }
                    for obligation in item.obligations
                ],
            }
            for item in plan.consumer_capability_impact
        ],
        "retained_server_ids": list(plan.retained_server_ids),
        "retired_server_ids": list(plan.retired_server_ids),
        "assembly_off_allowed": plan.assembly_off_allowed,
        "assembly_off_blockers": list(plan.assembly_off_blockers),
        "plan_fingerprint": plan.plan_fingerprint,
    }


def _retirement_evidence(
    decisions: Iterable[LegacyServerClassification],
    plan: LegacyMigrationPlan,
) -> list[dict[str, Any]]:
    evidence = []
    impact_by_server = {
        item.server_id: item for item in plan.consumer_capability_impact
    }
    for item in sorted(decisions, key=lambda decision: decision.server_id):
        if item.disposition is not LegacyDisposition.RETIRE:
            continue
        evidence.append(
            {
                "server_id": item.server_id,
                "approver": item.retirement_approver,
                "reason_fingerprint": _fingerprint(item.retirement_reason or ""),
                "impact_accepted": item.impact_accepted,
                "target_consumer_set_digest": impact_by_server[
                    item.server_id
                ].target_consumer_set_digest,
                "capability_obligations_fingerprint": _fingerprint(
                    [
                        {
                            "consumer_ref": obligation.consumer_ref,
                            "capability_id": obligation.capability_id,
                            "source_contract_fingerprint": (
                                obligation.source_contract_fingerprint
                            ),
                            "resolution": obligation.resolution.value,
                        }
                        for obligation in impact_by_server[item.server_id].obligations
                    ]
                ),
            }
        )
    return evidence


def _validate_retirement_evidence(value: Any, plan: LegacyMigrationPlan) -> None:
    if not isinstance(value, list):
        raise MigrationCommandError("retirement_evidence_invalid")
    ids = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise MigrationCommandError("retirement_evidence_invalid")
        server_id = item.get("server_id")
        if (
            not isinstance(server_id, str)
            or server_id in ids
            or not isinstance(item.get("approver"), str)
            or not item.get("approver", "").strip()
            or not isinstance(item.get("reason_fingerprint"), str)
            or item.get("impact_accepted") is not True
        ):
            raise MigrationCommandError("retirement_evidence_invalid")
        ids.add(server_id)
    if ids != set(plan.retired_server_ids):
        raise MigrationCommandError("retirement_evidence_incomplete")


def _health_results(value: Any) -> tuple[LegacyMigrationHealthResult, ...]:
    if not isinstance(value, list):
        raise MigrationCommandError("migration_health_evidence_invalid")
    if any(not isinstance(item, Mapping) for item in value):
        raise MigrationCommandError("migration_health_evidence_invalid")
    try:
        return tuple(LegacyMigrationHealthResult(**dict(item)) for item in value)
    except (TypeError, ValueError) as exc:
        raise MigrationCommandError("migration_health_evidence_invalid") from exc


def _health_unavailable(
    _config: MCPRuntimeConfig,
    _plan: LegacyMigrationPlan,
    _policy: LegacyMigrationHealthPolicy,
) -> Iterable[LegacyMigrationHealthResult]:
    return ()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or apply a legacy MCP configuration migration."
    )
    parser.add_argument(
        "--config", required=True, help="Legacy mcp_server_config.json path."
    )
    parser.add_argument(
        "--classifications", required=True, help="Explicit classification JSON path."
    )
    parser.add_argument(
        "--artifact-out", help="Write the dry-run artifact to this path."
    )
    parser.add_argument(
        "--artifact", help="Previously generated artifact required for --apply."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Explicitly request an idempotent apply."
    )
    parser.add_argument(
        "--database-path", help="Initialized local SQLite database path."
    )
    parser.add_argument(
        "--credential-key-file", help="Read-only 32-byte credential key file."
    )
    parser.add_argument(
        "--service-account-owner", help="Exact target service-account owner."
    )
    parser.add_argument(
        "--audit-out",
        help="Optional non-authoritative 0600 export referencing the durable record.",
    )
    parser.add_argument("--allowlist-domain", action="append", default=[])
    parser.add_argument("--allowlist-cidr", action="append", default=[])
    return parser


def _build_local_applier(
    args: argparse.Namespace,
    *,
    config: MCPRuntimeConfig,
    plan: LegacyMigrationPlan,
    live_health_validator: LegacyMigrationLiveHealthValidator | None,
    live_validator_provenance: str | None,
) -> ArtifactApplier:
    if not args.credential_key_file:
        raise MigrationCommandError("apply_backend_options_required")
    if not args.service_account_owner:
        raise MigrationCommandError("service_account_owner_required")
    from src.integrations.mcp.credentials import CredentialCipher
    from src.integrations.mcp.endpoint_policy import EndpointAllowlist, EndpointPolicy

    cipher = CredentialCipher.from_key_file(
        args.credential_key_file, require_read_only=True
    )
    state_config = build_state_platform_runtime_config(
        env=os.environ,
        require_driver=True,
    )
    if state_config.backend is StatePlatformBackend.POSTGRESQL:
        if args.database_path:
            raise MigrationCommandError("postgresql_database_path_forbidden")
        from src.storage.postgres import (
            PostgreSQLStorage,
            create_postgres_engine,
            create_postgres_session_factory,
        )
        from src.storage.postgres.session import (
            validate_mcp_legacy_migration_connection_role,
        )

        migration_dsn = (
            os.environ.get("MAF_MCP_LEGACY_MIGRATION_DSN") or ""
        ).strip()
        if not migration_dsn:
            raise MigrationCommandError("mcp_legacy_migration_dsn_required")
        try:
            migration_url = make_url(migration_dsn)
        except (ArgumentError, ValueError):
            raise MigrationCommandError("mcp_legacy_migration_dsn_invalid") from None
        migration_login = (migration_url.username or "").strip()
        if not migration_login:
            raise MigrationCommandError("mcp_legacy_migration_login_required")
        try:
            engine = create_postgres_engine(migration_dsn)
        except (ArgumentError, ValueError):
            raise MigrationCommandError("mcp_legacy_migration_dsn_invalid") from None
        try:
            validate_mcp_legacy_migration_connection_role(
                engine,
                migration_login,
            )
        except (RuntimeError, ValueError):
            engine.dispose()
            raise MigrationCommandError("mcp_legacy_migration_role_invalid") from None
        migration_session_factory = create_postgres_session_factory(engine)
        storage = PostgreSQLStorage(
            migration_session_factory,
            mcp_legacy_migration_session_factory=migration_session_factory,
            mcp_legacy_migration_role=migration_login,
        )
    else:
        if not args.database_path:
            raise MigrationCommandError("apply_backend_options_required")
        database_path = Path(args.database_path)
        if not database_path.is_file():
            raise MigrationCommandError("apply_database_unavailable")
        from src.storage.sqlite import (
            SQLiteStorage,
            create_sqlite_engine,
            create_sqlite_session_factory,
        )

        engine = create_sqlite_engine(database_path)
        storage = SQLiteStorage(create_sqlite_session_factory(engine))
    endpoint_policy = EndpointPolicy(
        allowlist=EndpointAllowlist.from_values(
            domains=args.allowlist_domain,
            cidrs=args.allowlist_cidr,
        )
    )
    selected_validator = (
        live_health_validator
        or BuiltInLegacyMigrationLiveHealthValidator(endpoint_policy)
    )
    selected_provenance = live_validator_provenance or getattr(
        selected_validator, "provenance", None
    )
    applier = LocalLegacyMigrationApplier(
        storage=storage,
        credential_cipher=cipher,
        endpoint_policy=endpoint_policy,
        config=config,
        plan=plan,
        service_account_owner=args.service_account_owner,
        live_health_validator=selected_validator,
        validator_provenance=selected_provenance,
    )

    def apply(artifact: Mapping[str, Any], *, idempotency_key: str) -> str:
        try:
            return applier(artifact, idempotency_key=idempotency_key)
        finally:
            engine.dispose()

    return apply


def _write_audit(
    path: Path, result: Mapping[str, Any], plan: LegacyMigrationPlan
) -> None:
    payload = {
        "schema": "maf.legacy_mcp_migration.audit_export.v2",
        "role": "non_authoritative_export",
        "authority_store": "mcp_legacy_migration_record",
        **dict(result),
        "targets": [
            {
                "durable_record_id": legacy_migration_record_id(
                    plan_fingerprint=plan.plan_fingerprint,
                    source_server_id=candidate.source_server_id,
                    target_server_id=candidate.target_server_id,
                ),
                "event_type": "mcp.legacy.config_migrated",
                "owner_consumer_ref": candidate.owner_consumer_ref,
                "server_id": candidate.target_server_id,
                "source_fingerprint": candidate.source_fingerprint,
            }
            for candidate in plan.mapping_candidates
        ],
        "retired_server_ids": list(plan.retired_server_ids),
        "runtime_state_migrated": False,
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except OSError:
        raise MigrationCommandError("migration_audit_write_failed") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _load_json_object(path: Path, error: str) -> dict[str, Any]:
    payload = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
    )
    if not isinstance(payload, dict):
        raise MigrationCommandError(error)
    return payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MigrationCommandError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _write_artifact(path: Path, artifact: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MigrationCommandError("legacy_classification_string_invalid")
    return value


def _safe_consumer_refs(value: Any, *, server_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise MigrationCommandError(
            f"legacy_classification_target_consumers_invalid:{server_id}"
        )
    return tuple(value)


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.prd_evidence import (  # noqa: E402
    EvidenceError,
    GateSpec,
    allowed_digest_sets,
    collect_gate_results,
    finish_release_gate_result,
    is_pending,
    required_mapping,
    run_evidence_cli,
    validate_schema_version,
)
from src.storage.runtime_sidecar_facade import (  # noqa: E402
    load_runtime_sidecar_migration_evidence_artifact,
    validate_runtime_sidecar_artifact_provenance,
    validate_runtime_sidecar_benchmark_report,
    validate_runtime_sidecar_decommission_readiness,
    validate_runtime_sidecar_migration_plan,
    validate_runtime_sidecar_ops_readiness,
    validate_runtime_sidecar_promotion_readiness,
)
from src.storage.rust_contract import migration_policy  # noqa: E402


DEFAULT_EVIDENCE = Path("docs/prd/rust/evidence/prd03/runtime_sidecar_release_gates.json")
SCHEMA_VERSION = "maf.prd03.runtime_sidecar_evidence.v1"
INVALID_CODE = "prd03_runtime_sidecar_evidence_invalid"
PENDING_CODE = "prd03_runtime_sidecar_evidence_pending"


def _validate_artifact(payload: Mapping[str, Any]) -> dict[str, str]:
    metadata = payload.get("artifact_provenance")
    if is_pending(metadata):
        raise EvidenceError(PENDING_CODE, "artifact provenance evidence is pending")
    allowed_checksums, allowed_cargo_lock_digests = allowed_digest_sets(payload, invalid_code=INVALID_CODE)
    return validate_runtime_sidecar_artifact_provenance(
        metadata,
        allowed_checksums=allowed_checksums,
        allowed_cargo_lock_digests=allowed_cargo_lock_digests,
    )


def validate_evidence(payload: Mapping[str, Any], *, allow_pending: bool) -> dict[str, Any]:
    validate_schema_version(payload, expected=SCHEMA_VERSION, invalid_code=INVALID_CODE)

    gate_specs: tuple[GateSpec, ...] = (
        ("artifact_provenance", lambda: _validate_artifact(payload)),
        (
            "benchmark_report",
            lambda: required_mapping(
                payload,
                "benchmark_report",
                validate_runtime_sidecar_benchmark_report,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "promotion_readiness",
            lambda: required_mapping(
                payload,
                "promotion_readiness",
                validate_runtime_sidecar_promotion_readiness,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "migration_plan",
            lambda: required_mapping(
                payload,
                "migration_plan",
                validate_runtime_sidecar_migration_plan,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "ops_readiness",
            lambda: required_mapping(
                payload,
                "ops_readiness",
                validate_runtime_sidecar_ops_readiness,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "decommission_readiness",
            lambda: required_mapping(
                payload,
                "decommission_readiness",
                validate_runtime_sidecar_decommission_readiness,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
    )
    results, pending = collect_gate_results(gate_specs, allow_pending=allow_pending, pending_code=PENDING_CODE)
    return finish_release_gate_result(
        payload,
        results=results,
        pending=pending,
        allow_pending=allow_pending,
        pending_code=PENDING_CODE,
    )


def main() -> int:
    if "--task-authority-cutover-evidence" in sys.argv:
        parser = argparse.ArgumentParser(
            description=(
                "Validate authenticated RuntimeSidecar Task and Submission authority "
                "migration evidence."
            )
        )
        parser.add_argument("--task-authority-cutover-evidence", type=Path, required=True)
        parser.add_argument("--hmac-key", type=Path)
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args()
        policy = migration_policy()
        key_path = args.hmac_key or Path(
            os.environ.get(policy["task_authority_hmac_key_path_env"], "")
        )
        try:
            result = load_runtime_sidecar_migration_evidence_artifact(
                args.task_authority_cutover_evidence,
                authentication_key_path=key_path,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print("runtime_sidecar_task_submission_authority_migration_evidence_ready")
        return 0
    return run_evidence_cli(
        validate_evidence,
        default_evidence=DEFAULT_EVIDENCE,
        description="Validate PRD03 RuntimeSidecar release-gate evidence.",
        invalid_code=INVALID_CODE,
        missing_pending_code=PENDING_CODE,
        status_messages={"ready": "prd03_runtime_sidecar_evidence_ready"},
        pending_message_prefix="prd03_runtime_sidecar_evidence_pending",
    )


if __name__ == "__main__":
    raise SystemExit(main())

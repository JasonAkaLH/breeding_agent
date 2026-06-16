from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

GateCheck = Callable[[], dict[str, Any]]
GateSpec = tuple[str, GateCheck]
StatusMessages = Mapping[str, str]


class EvidenceValidator(Protocol):
    def __call__(self, payload: Mapping[str, Any], *, allow_pending: bool) -> dict[str, Any]: ...


class EvidenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def load_json_object(path: Path, *, invalid_code: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError(invalid_code, f"{path} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EvidenceError(invalid_code, f"{path} must contain a JSON object")
    return payload


def validate_schema_version(payload: Mapping[str, Any], *, expected: str, invalid_code: str) -> None:
    if payload.get("schema_version") != expected:
        raise EvidenceError(invalid_code, "unsupported evidence schema_version")


def is_pending(value: Any) -> bool:
    return value is None or value == {} or (isinstance(value, Mapping) and value.get("status") == "pending")


def require_mapping(payload: Mapping[str, Any], key: str, *, invalid_code: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise EvidenceError(invalid_code, f"{key} must be a JSON object")
    return value


def required_mapping(
    payload: Mapping[str, Any],
    key: str,
    validator: Callable[[Mapping[str, Any]], dict[str, Any]],
    *,
    pending_code: str,
    invalid_code: str,
) -> dict[str, Any]:
    value = payload.get(key)
    if is_pending(value):
        raise EvidenceError(pending_code, f"{key} evidence is pending")
    if not isinstance(value, Mapping):
        raise EvidenceError(invalid_code, f"{key} must be a JSON object")
    return validator(value)


def require_true_flags(
    mapping: Mapping[str, Any],
    keys: Sequence[str],
    *,
    parent: str,
    invalid_code: str,
) -> dict[str, bool]:
    missing = [key for key in keys if mapping.get(key) is not True]
    if missing:
        raise EvidenceError(invalid_code, f"{parent} booleans must be true: " + ",".join(missing))
    return {key: True for key in keys}


def allowed_digest_sets(payload: Mapping[str, Any], *, invalid_code: str) -> tuple[set[str], set[str]]:
    allowed_checksums = payload.get("allowed_artifact_checksums", [])
    allowed_cargo_lock_digests = payload.get("allowed_cargo_lock_digests", [])
    if not isinstance(allowed_checksums, list) or not isinstance(allowed_cargo_lock_digests, list):
        raise EvidenceError(
            invalid_code,
            "allowed artifact checksum and Cargo.lock digest lists are required",
        )
    return {str(item) for item in allowed_checksums}, {str(item) for item in allowed_cargo_lock_digests}


def collect_gate_results(
    gate_specs: Sequence[GateSpec],
    *,
    allow_pending: bool,
    pending_code: str,
) -> tuple[dict[str, Any], list[str]]:
    results: dict[str, Any] = {}
    pending: list[str] = []
    for gate, check in gate_specs:
        try:
            results[gate] = check()
        except EvidenceError as exc:
            if exc.code == pending_code and allow_pending:
                pending.append(gate)
                results[gate] = {"status": "pending", "reason": str(exc)}
                continue
            raise
    return results, pending


def finish_release_gate_result(
    payload: Mapping[str, Any],
    *,
    results: dict[str, Any],
    pending: list[str],
    allow_pending: bool,
    pending_code: str,
    blockers_key: str = "blockers",
    blockers_label: str = "external blockers remain",
) -> dict[str, Any]:
    blockers = payload.get(blockers_key, [])
    if blockers and not allow_pending:
        raise EvidenceError(pending_code, blockers_label + ": " + ", ".join(str(item) for item in blockers))
    if pending and not allow_pending:
        raise EvidenceError(pending_code, "pending gates remain: " + ", ".join(pending))
    return {
        "status": "ready" if not pending and not blockers else "pending",
        "pending_gates": pending,
        blockers_key: blockers,
        "results": results,
    }


def build_evidence_parser(
    *,
    description: str,
    default_evidence: Path,
    allow_pending_help: str = "Treat missing external production evidence as a non-release-blocking pending status.",
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--evidence", type=Path, default=default_evidence)
    parser.add_argument("--allow-pending", action="store_true", help=allow_pending_help)
    parser.add_argument("--json", action="store_true", help="Print machine-readable validation result.")
    return parser


def run_evidence_cli(
    validate_evidence: EvidenceValidator,
    *,
    default_evidence: Path,
    description: str,
    invalid_code: str,
    missing_pending_code: str,
    status_messages: StatusMessages,
    pending_message_prefix: str,
    success_statuses: Sequence[str] = ("ready",),
    blockers_key: str = "blockers",
    allow_pending_help: str = "Treat missing external production evidence as a non-release-blocking pending status.",
    catch_runtime_error: bool = True,
) -> int:
    parser = build_evidence_parser(
        description=description,
        default_evidence=default_evidence,
        allow_pending_help=allow_pending_help,
    )
    args = parser.parse_args()
    try:
        if not args.evidence.exists():
            if args.allow_pending:
                result = {
                    "status": "pending",
                    "pending_gates": ["evidence_file"],
                    blockers_key: [f"{args.evidence} does not exist"],
                    "results": {},
                }
            else:
                raise EvidenceError(missing_pending_code, f"{args.evidence} does not exist")
        else:
            result = validate_evidence(
                load_json_object(args.evidence, invalid_code=invalid_code),
                allow_pending=args.allow_pending,
            )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif result["status"] in status_messages:
            print(status_messages[result["status"]])
        else:
            print(pending_message_prefix + ": " + ",".join(result.get("pending_gates", [])))
        return 0 if result["status"] in success_statuses or args.allow_pending else 1
    except EvidenceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RuntimeError as exc:
        if not catch_runtime_error:
            raise
        print(str(exc), file=sys.stderr)
        return 1


def load_lightweight_module(
    *,
    package_name: str,
    module_dir: Path,
    name: str,
    error_label: str,
) -> Any:
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(module_dir)]  # type: ignore[attr-defined]
        sys.modules[package_name] = package

    module_name = f"{package_name}.{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_dir / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {error_label} helper module: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
